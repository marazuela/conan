"""
Modal app definition for Conan v2.

Surface (Phase 3 — full scanner fleet):
  - `rubric_apply_caps`  — web endpoint RPC'd by the reactor edge function on every
    signals.INSERT to apply auto-caps without porting rubric logic to TypeScript.
  - `health`             — trivial GET for dashboard + smoke tests.
  - 17 scanner functions — each as `<name>_once` (on-demand callable).
  - 3 dispatcher crons   — `dispatch_3h`, `dispatch_daily`, `dispatch_weekly`. Each
    fires on the bucketed schedule and `.spawn()`s every `_once` in its bucket.
    This keeps us under Modal's 5-cron plan limit while preserving per-scanner
    isolation (each scanner runs in its own container with its own timeout).

NOT hosted here (by design, spec.md §7.4 revised 2026-04-20):
  - `thesis_writer`      — runs as a Claude skill under Pedro's account via a Cowork
    scheduled task (see `.claude/skills/thesis_writer.md`). Modal doesn't draft theses.
  - `candidate_aging`    — same pattern; a separate skill.

Active scheduled subset (operator bandwidth cap, 2026-04-22):
  - 3h:      edgar_filing_monitor, fda_pdufa_pipeline
  - weekly:  takeover_candidate_scanner
  - daily fetchers (universe maintenance, not signal emitters):
             fda_adcomm_pdufa, sec_8k_mna

All other scanner `_once` functions remain deployed for manual/on-demand use,
but are intentionally omitted from the scheduled dispatch buckets until they are
re-enabled in the registry/operator UI.

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
# anthropic-secrets intentionally NOT referenced here. thesis_writer + candidate_aging
# run as Claude skills via Cowork scheduled tasks, not as Modal functions.


# ----------------------------------------------------------------------
# rubric_apply_caps — called by the reactor edge function on every signals.INSERT.
# Wraps the Python apply_auto_caps so no rubric logic is ported to TypeScript.
# ----------------------------------------------------------------------

@app.function(image=image, timeout=10)
@modal.fastapi_endpoint(method="POST", label="rubric-apply-caps")
def rubric_apply_caps(payload: dict) -> dict:
    from modal_workers.shared.rubric_engine import apply_auto_caps
    signal = payload.get("signal") or {}
    dimensions = payload.get("dimensions") or {}
    profile = payload["profile"]
    band = payload["band"]
    new_band, caps = apply_auto_caps(signal, dimensions, profile, band)
    return {"band": new_band, "auto_caps_triggered": caps}


# ----------------------------------------------------------------------
# health — trivial liveness check; doubles as smoke test after deploy.
# ----------------------------------------------------------------------

@app.function(image=image, timeout=5)
@modal.fastapi_endpoint(method="GET", label="health")
def health() -> dict:
    from modal_workers.shared.rubric_engine import WEIGHTS
    return {
        "status": "ok",
        "app": "conan-v2",
        "profiles": sorted(WEIGHTS.keys()),
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

@app.function(image=image, timeout=120, secrets=[scanner_secrets, supabase_secrets])
def fda_pdufa_pipeline_once() -> dict:
    return _run("fda_pdufa_pipeline")

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


# ==========================================================================
# Catalyst-universe fetchers (Phase 1b of the accuracy feedback loop).
# Populate catalyst_universe with independent-truth catalyst events, which
# the coverage_auditor (Cowork weekly skill) joins against emissions_ledger
# to identify recall gaps.
# ==========================================================================

@app.function(image=image, timeout=300, secrets=[scanner_secrets, supabase_secrets])
def fda_adcomm_pdufa_once() -> dict:
    """openFDA drugsfda AP submissions → catalyst_universe (fda_approval).
    Default 7-day look-back; overridden by dispatch_daily's schedule."""
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

_SCANNERS_3H: List[str] = [
    "edgar_filing_monitor", "fda_pdufa_pipeline",
]
_SCANNERS_DAILY: List[str] = []
_SCANNERS_WEEKLY: List[str] = [
    "takeover_candidate_scanner",
]

# Catalyst-universe fetchers run alongside daily scanners. They use the same
# `_once` spawn pattern — _dispatch looks up `<name>_once` in this module.
# Folded into dispatch_daily because the Modal free-tier 5-cron limit is at
# capacity (dispatch_3h, dispatch_daily, dispatch_weekly, dispatch_observability,
# reporting_weekly_cron).
_FETCHERS_DAILY: List[str] = [
    "fda_adcomm_pdufa",
    "sec_8k_mna",
]


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
    return _dispatch(_SCANNERS_3H)


@app.function(image=image, schedule=modal.Cron("0 9 * * *"), timeout=60,
              secrets=[scanner_secrets, supabase_secrets])
def dispatch_daily() -> dict:
    return _dispatch(_SCANNERS_DAILY + _FETCHERS_DAILY)


@app.function(image=image, schedule=modal.Cron("0 12 * * 1"), timeout=60,
              secrets=[scanner_secrets, supabase_secrets])
def dispatch_weekly() -> dict:
    return _dispatch(_SCANNERS_WEEKLY)


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
        precision_auditor, scanner_probe, timing_auditor, translation_health,
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

    # Always: deterministic pre-edge lifecycle guard.
    try:
        results["pre_edge_monitor"] = pre_edge_monitor()
        results["ran"].append("pre_edge_monitor")
    except Exception as e:
        results["pre_edge_monitor_error"] = str(e)

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


