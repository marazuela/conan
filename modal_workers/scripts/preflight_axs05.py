"""Gate 2B pre-flight verifier for the v3 orchestrator on a specific FDA asset.

Runs end-to-end checks BEFORE you spend money on a Tier-1 record run. Each
check is independent — one failure doesn't crash the others, so a single
invocation surfaces every blocker in one pass.

Exit code:
  0  — all required checks pass (warnings are non-blocking).
  1  — at least one required check failed; see the table for which.

Usage:
  python -m modal_workers.scripts.preflight_axs05 --asset-id <uuid>
  python -m modal_workers.scripts.preflight_axs05 --ticker AXSM
  python -m modal_workers.scripts.preflight_axs05 --ticker AXSM --json

Required env (read by SupabaseClient):
  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY

Optional env:
  POLYGON_API_KEY        — absent triggers a warning (degraded options
                           sub-agent), not a failure.
  ORCH_E2E_MAX_COST_USD  — informational; surfaces what the live run will
                           hard-kill at. Default 15.0 per D-125.

Plan ref: ~/.claude/plans/plan-it-for-optimal-twinkling-bubble.md G2B.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Glyphs land in -t terminals; --json strips them.
GLYPH = {"pass": "✓", "warn": "⚠", "fail": "✗", "skip": "·"}
COLOR = {
    "pass": "\033[32m",
    "warn": "\033[33m",
    "fail": "\033[31m",
    "skip": "\033[90m",
    "reset": "\033[0m",
}


@dataclass
class CheckResult:
    name: str
    status: str       # "pass" | "warn" | "fail" | "skip"
    detail: str = ""
    hint: str = ""
    elapsed_ms: int = 0
    blocking: bool = True   # warns + skips are never blocking; "fail" + blocking=True → exit 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _result(name: str, status: str, *, detail: str = "", hint: str = "", elapsed_ms: int = 0,
            blocking: bool = True) -> CheckResult:
    return CheckResult(name=name, status=status, detail=detail, hint=hint,
                       elapsed_ms=elapsed_ms, blocking=blocking)


def _timed(fn: Callable[[], CheckResult]) -> CheckResult:
    """Run a check and stamp the elapsed_ms field."""
    t0 = time.time()
    try:
        r = fn()
    except Exception as exc:  # noqa: BLE001
        return _result(
            name=getattr(fn, "__name__", "<unknown>"),
            status="fail",
            detail=f"{type(exc).__name__}: {exc}",
            hint="check raised — investigate the traceback above",
            elapsed_ms=int((time.time() - t0) * 1000),
        )
    r.elapsed_ms = int((time.time() - t0) * 1000)
    return r


def _modal_cli_ok() -> bool:
    try:
        out = subprocess.run(
            ["modal", "--version"], capture_output=True, text=True, timeout=10,
        )
        return out.returncode == 0
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_modal_secret() -> CheckResult:
    """Gate 0.1: anthropic-orchestrator secret exists."""
    if not _modal_cli_ok():
        return _result("modal secret: anthropic-orchestrator",
                       status="skip", blocking=False,
                       detail="modal CLI not on PATH — cannot verify",
                       hint="install + auth: `pip install modal && modal token new`")
    try:
        out = subprocess.run(
            ["modal", "secret", "list"], capture_output=True, text=True, timeout=15,
        )
    except Exception as exc:  # noqa: BLE001
        return _result("modal secret: anthropic-orchestrator",
                       status="fail", detail=str(exc),
                       hint="`modal secret list` failed — auth issue?")
    found = "anthropic-orchestr" in out.stdout
    if found:
        return _result("modal secret: anthropic-orchestrator",
                       status="pass", detail="present in workspace")
    return _result("modal secret: anthropic-orchestrator",
                   status="fail",
                   detail="not in `modal secret list` output",
                   hint="`modal secret create anthropic-orchestrator ANTHROPIC_API_KEY=...`")


def check_modal_app_deployed() -> CheckResult:
    """Gate 0: conan-v3-orchestrator app deployed."""
    if not _modal_cli_ok():
        return _result("modal app: conan-v3-orchestrator",
                       status="skip", blocking=False,
                       detail="modal CLI not on PATH")
    try:
        out = subprocess.run(
            ["modal", "app", "list"], capture_output=True, text=True, timeout=15,
        )
    except Exception as exc:  # noqa: BLE001
        return _result("modal app: conan-v3-orchestrator",
                       status="fail", detail=str(exc))
    # Modal truncates names in the table — match a prefix.
    deployed = any(
        ("conan-v3-or" in line and "deployed" in line)
        for line in out.stdout.splitlines()
    )
    if deployed:
        return _result("modal app: conan-v3-orchestrator",
                       status="pass", detail="deployed state in `modal app list`")
    return _result("modal app: conan-v3-orchestrator",
                   status="fail",
                   detail="no deployed conan-v3-orchestrator app",
                   hint="`modal deploy modal_workers/orchestrator_app.py`")


def check_supabase_connectivity() -> CheckResult:
    """Verify SUPABASE_URL + SERVICE_ROLE_KEY work."""
    try:
        from modal_workers.shared.supabase_client import SupabaseClient
    except Exception as exc:  # noqa: BLE001
        return _result("supabase connectivity",
                       status="fail",
                       detail=f"import failed: {exc}",
                       hint="run from repo root with PYTHONPATH=.")
    try:
        sb = SupabaseClient()
        # Ping the cheapest endpoint — list scanners with limit 1.
        rows = sb._rest("GET", "scanners", params={"select": "id", "limit": "1"}) or []
        return _result("supabase connectivity",
                       status="pass",
                       detail=f"reachable; sample query returned {len(rows)} row(s)")
    except Exception as exc:  # noqa: BLE001
        return _result("supabase connectivity",
                       status="fail",
                       detail=f"{type(exc).__name__}: {exc}",
                       hint="check SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY env")


def check_rag_corpus_populated(sb_factory: Callable[[], Any]) -> CheckResult:
    """Gate 1A exit: each chunk_embeddings_<corpus> has > 0 rows."""
    try:
        from modal_workers.rag.hybrid_search import CORPUS_TABLE
    except Exception as exc:  # noqa: BLE001
        return _result("RAG corpus rowcounts",
                       status="fail",
                       detail=f"import failed: {exc}")
    try:
        sb = sb_factory()
    except Exception as exc:  # noqa: BLE001
        return _result("RAG corpus rowcounts",
                       status="skip", blocking=False,
                       detail=f"sb unavailable: {exc}")
    counts: Dict[str, int] = {}
    for corpus, table in CORPUS_TABLE.items():
        try:
            rows = sb._rest("GET", table, params={"select": "id", "limit": "1"},
                            headers={"Prefer": "count=exact"})
            # PostgREST returns `Content-Range` header for exact counts; we
            # don't have header access from `_rest`, so fall back to a HEAD-
            # style approximation: count returned rows. If the table is empty,
            # rows is []; if populated, we still only got 1 row back. Use a
            # cheap second-step probe with `limit=0` and `Prefer: count=exact`.
            rows2 = sb._rest("GET", table, params={"select": "id", "limit": "0"})
            counts[corpus] = -1 if rows2 is None else (len(rows) + len(rows2))
            # Replace approximate-count probe with an explicit count query.
            count_rows = sb._rest("GET", table, params={"select": "id"}) or []
            counts[corpus] = len(count_rows)
        except Exception as exc:  # noqa: BLE001
            return _result("RAG corpus rowcounts",
                           status="fail",
                           detail=f"{corpus}: {exc}",
                           hint=f"table {table} may not exist — check 20260510000000_v3_rag_infrastructure.sql applied")
    empty = [c for c, n in counts.items() if n == 0]
    summary = ", ".join(f"{c}={n}" for c, n in counts.items())
    if empty:
        return _result("RAG corpus rowcounts",
                       status="fail",
                       detail=summary,
                       hint=f"empty: {empty}. run `modal run modal_workers/scripts/backfill_rag_corpus.py`")
    return _result("RAG corpus rowcounts",
                   status="pass",
                   detail=summary)


def check_rag_hybrid_search_smoke(sb_factory: Callable[[], Any], ticker: str) -> CheckResult:
    """Run a real hybrid_search and confirm ≥1 chunk comes back."""
    try:
        from orchestrator_runtime.rag_handle import hybrid_search
    except Exception as exc:  # noqa: BLE001
        return _result("RAG hybrid_search smoke",
                       status="fail", detail=f"import failed: {exc}")
    try:
        sb = sb_factory()
    except Exception as exc:  # noqa: BLE001
        return _result("RAG hybrid_search smoke",
                       status="skip", blocking=False, detail=str(exc))
    query = f"PDUFA {ticker}".strip()
    try:
        chunks = hybrid_search(sb, query, k=8)
    except Exception as exc:  # noqa: BLE001
        return _result("RAG hybrid_search smoke",
                       status="fail",
                       detail=f"hybrid_search raised: {exc}",
                       hint="check rerank-2.5 + voyage embed providers in `rag-providers` secret")
    n = len(chunks or [])
    if n == 0:
        return _result("RAG hybrid_search smoke",
                       status="warn", blocking=False,
                       detail=f"0 chunks for query={query!r}",
                       hint="corpus may be populated but lacks coverage for this asset; "
                            "try a broader query or check Voyage embedder is online")
    return _result("RAG hybrid_search smoke",
                   status="pass",
                   detail=f"{n} chunks returned for query={query!r}")


def check_sub_agent_stack() -> CheckResult:
    """All 5 sub-agent runners importable + ROLE_REGISTRY complete."""
    try:
        from modal_workers.sub_agents import ROLE_REGISTRY
    except Exception as exc:  # noqa: BLE001
        return _result("sub-agent stack",
                       status="fail",
                       detail=f"import failed: {exc}")
    expected = {"literature", "competitive", "regulatory_history",
                "options_microstructure", "ic_memo"}
    missing = expected - set(ROLE_REGISTRY.keys())
    if missing:
        return _result("sub-agent stack",
                       status="fail",
                       detail=f"ROLE_REGISTRY missing: {sorted(missing)}",
                       hint="check modal_workers/sub_agents/__init__.py imports all 5 runners")
    # Construct each + verify build_handler returns a callable.
    bad: List[str] = []
    for role, runner_cls in ROLE_REGISTRY.items():
        try:
            handler = runner_cls().build_handler()
            if not callable(handler):
                bad.append(f"{role}:not_callable")
        except Exception as exc:  # noqa: BLE001
            bad.append(f"{role}:{type(exc).__name__}")
    if bad:
        return _result("sub-agent stack",
                       status="fail",
                       detail=f"build_handler failed: {bad}")
    return _result("sub-agent stack",
                   status="pass",
                   detail=f"5 runners registered: {sorted(ROLE_REGISTRY.keys())}")


def check_mcp_modules_importable() -> CheckResult:
    """All 8 MCP server modules importable (smoke; no live tool calls)."""
    plugin_dir = Path(__file__).resolve().parents[2] / "conan-fda-orchestrator-plugin" / "mcp_servers"
    if not plugin_dir.exists():
        return _result("MCP server modules",
                       status="fail",
                       detail=f"plugin dir missing: {plugin_dir}")
    try:
        import importlib
        if str(plugin_dir) not in sys.path:
            sys.path.insert(0, str(plugin_dir))
        # mcp[cli] required for FastMCP — without it, modules raise on import.
        try:
            import mcp  # noqa: F401
        except ImportError:
            return _result("MCP server modules",
                           status="warn", blocking=False,
                           detail="`mcp` package not installed",
                           hint="`pip install 'mcp[cli]'` — required for Cowork bulk path")
        names = ["pubmed_mcp", "biorxiv_mcp", "clinicaltrials_mcp",
                 "openfda_mcp", "fda_adcomm_mcp", "polygon_mcp",
                 "internal_rag_mcp", "compute_mcp"]
        missing: List[str] = []
        for n in names:
            try:
                mod = importlib.import_module(n)
                if not hasattr(mod, "mcp"):
                    missing.append(f"{n}:no_mcp_attr")
            except Exception as exc:  # noqa: BLE001
                missing.append(f"{n}:{type(exc).__name__}")
        if missing:
            return _result("MCP server modules",
                           status="fail",
                           detail=f"failed: {missing}")
        return _result("MCP server modules",
                       status="pass",
                       detail="8/8 importable + expose `mcp` instance")
    except Exception as exc:  # noqa: BLE001
        return _result("MCP server modules",
                       status="fail", detail=f"{type(exc).__name__}: {exc}")


def check_asset_row(sb_factory: Callable[[], Any], asset_id: str) -> Tuple[CheckResult, Optional[Dict[str, Any]]]:
    """fda_assets row exists, is_active=true, has memory_path/indication/application_number."""
    try:
        sb = sb_factory()
    except Exception as exc:  # noqa: BLE001
        return _result("asset row sanity", status="skip", blocking=False, detail=str(exc)), None
    try:
        rows = sb._rest("GET", "fda_assets", params={
            "select": "id,ticker,drug_name,indication,application_number,memory_path,"
                      "is_active,watch_priority,reference_class_signature",
            "id": f"eq.{asset_id}",
            "limit": "1",
        }) or []
    except Exception as exc:  # noqa: BLE001
        return _result("asset row sanity", status="fail", detail=str(exc)), None
    if not rows:
        return _result("asset row sanity",
                       status="fail",
                       detail=f"no fda_assets row for id={asset_id}",
                       hint="re-check the asset uuid; or run the watchlist seeder"), None
    asset = rows[0]
    issues: List[str] = []
    if not asset.get("is_active"):
        issues.append("is_active=false (orchestrator skips inactive)")
    for col in ("indication", "application_number"):
        if not asset.get(col):
            issues.append(f"{col} is null")
    if not asset.get("memory_path"):
        issues.append("memory_path is null (Stage 0 falls back to default)")
    if issues:
        status = "warn" if all("memory_path" in i or "watch_priority" in i for i in issues) else "fail"
        return _result("asset row sanity",
                       status=status,
                       blocking=(status == "fail"),
                       detail=f"{asset.get('ticker')}: {issues}",
                       hint="patch the asset row before firing the orchestrator"), asset
    return _result("asset row sanity",
                   status="pass",
                   detail=f"{asset.get('ticker')} / {asset.get('drug_name')} active, "
                          f"watch_priority={asset.get('watch_priority')}"), asset


def check_stage1_fuel(sb_factory: Callable[[], Any], asset_id: str) -> CheckResult:
    """Stage 1 needs ≥3 extracted_facts AND ≥3 asset_documents to be useful."""
    try:
        sb = sb_factory()
    except Exception as exc:  # noqa: BLE001
        return _result("stage 1 fuel", status="skip", blocking=False, detail=str(exc))
    try:
        facts = sb._rest("GET", "extracted_facts", params={
            "select": "id", "asset_id": f"eq.{asset_id}", "limit": "10",
        }) or []
        docs = sb._rest("GET", "asset_documents", params={
            "select": "id", "asset_id": f"eq.{asset_id}",
            "is_material": "eq.true", "limit": "10",
        }) or []
    except Exception as exc:  # noqa: BLE001
        return _result("stage 1 fuel", status="fail", detail=str(exc))
    n_facts, n_docs = len(facts), len(docs)
    detail = f"facts={n_facts}, asset_documents (material)={n_docs}"
    if n_facts < 3 or n_docs < 3:
        return _result("stage 1 fuel",
                       status="warn",
                       blocking=False,
                       detail=detail,
                       hint="Stage 1 may produce thin synthesis. Run extractor + asset_linker "
                            "or trigger ingestion adapters before recording the cassette.")
    return _result("stage 1 fuel",
                   status="pass",
                   detail=detail)


def check_polygon_mode() -> CheckResult:
    """POLYGON_API_KEY present → live; absent → degraded-mode warning."""
    if os.environ.get("POLYGON_API_KEY"):
        return _result("polygon options data",
                       status="pass",
                       detail="POLYGON_API_KEY set; options sub-agent runs live")
    return _result("polygon options data",
                   status="warn",
                   blocking=False,
                   detail="POLYGON_API_KEY unset",
                   hint="options sub-agent will return status='degraded' (per G1C). "
                        "Acceptable for v0; set the key when ready for live IV reads.")


def check_active_calibration_curve(sb_factory: Callable[[], Any]) -> CheckResult:
    """Stage 8 needs an active calibration_curves row to fill conviction_pct_calibrated."""
    try:
        sb = sb_factory()
    except Exception as exc:  # noqa: BLE001
        return _result("active calibration curve", status="skip", blocking=False, detail=str(exc))
    try:
        rows = sb._rest("GET", "calibration_curves", params={
            "select": "version,n_training_samples,brier_score,fitted_at",
            "is_active": "eq.true", "limit": "1",
        }) or []
    except Exception as exc:  # noqa: BLE001
        return _result("active calibration curve", status="fail", detail=str(exc))
    if not rows:
        return _result("active calibration curve",
                       status="warn",
                       blocking=False,
                       detail="no is_active=true row",
                       hint="cold start. Stage 8 will pass-through raw_conviction_pct as "
                            "conviction_pct_calibrated. First nightly_calibration_refit "
                            "post-Gate-4 will install version #1.")
    r = rows[0]
    return _result("active calibration curve",
                   status="pass",
                   detail=f"version={r.get('version')}, n={r.get('n_training_samples')}, "
                          f"brier={r.get('brier_score')}")


def check_cassette_dir_writable() -> CheckResult:
    """Cassette dir from G2C must exist or be creatable."""
    target = Path("modal_workers/tests/fixtures/cassettes")
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".preflight_probe"
        probe.write_text("ok")
        probe.unlink()
    except Exception as exc:  # noqa: BLE001
        return _result("cassette dir writable",
                       status="fail",
                       detail=str(exc),
                       hint=f"create + chmod {target}")
    return _result("cassette dir writable",
                   status="pass", detail=str(target))


def check_cost_ceiling_visible() -> CheckResult:
    """Surface the env-tunable cost ceiling so operator sees what hard-kill applies."""
    val = os.environ.get("ORCH_E2E_MAX_COST_USD")
    if val:
        return _result("cost ceiling (informational)",
                       status="pass",
                       detail=f"ORCH_E2E_MAX_COST_USD=${val} (D-125 hard kill at this value)")
    return _result("cost ceiling (informational)",
                   status="pass",
                   detail="default $15.00 hard kill (D-125)",
                   hint="override with ORCH_E2E_MAX_COST_USD if you want a tighter cap")


# ---------------------------------------------------------------------------
# Asset id resolver
# ---------------------------------------------------------------------------


def resolve_asset(asset_id: Optional[str], ticker: Optional[str], sb_factory) -> Tuple[Optional[str], Optional[str]]:
    """Return (asset_id, ticker_for_query). Pass either the asset_id directly
    or a ticker; the latter resolves to the latest active fda_assets row."""
    if asset_id:
        return asset_id, ticker or ""
    if not ticker:
        return None, None
    try:
        sb = sb_factory()
        rows = sb._rest("GET", "fda_assets", params={
            "select": "id,ticker", "ticker": f"eq.{ticker}",
            "is_active": "eq.true",
            "order": "updated_at.desc", "limit": "1",
        }) or []
        if not rows:
            return None, ticker
        return rows[0]["id"], rows[0]["ticker"]
    except Exception:  # noqa: BLE001
        return None, ticker


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def run_all(*, asset_id: Optional[str], ticker: Optional[str]) -> List[CheckResult]:
    """Run every check, returning the ordered result list."""
    from modal_workers.shared.supabase_client import SupabaseClient

    _sb_singleton: Dict[str, Any] = {}

    def sb_factory() -> Any:
        if "sb" not in _sb_singleton:
            _sb_singleton["sb"] = SupabaseClient()
        return _sb_singleton["sb"]

    results: List[CheckResult] = []

    # Modal layer
    results.append(_timed(check_modal_secret))
    results.append(_timed(check_modal_app_deployed))

    # Supabase + corpus
    results.append(_timed(check_supabase_connectivity))
    results.append(_timed(lambda: check_rag_corpus_populated(sb_factory)))

    # Resolve asset_id from ticker if needed.
    resolved_id, resolved_ticker = resolve_asset(asset_id, ticker, sb_factory)
    if not resolved_id:
        results.append(_result(
            "asset resolution",
            status="fail",
            detail=f"could not resolve asset for asset_id={asset_id!r} ticker={ticker!r}",
            hint="pass --asset-id <uuid> or --ticker <symbol> matching an active fda_assets row",
        ))
    else:
        results.append(_result(
            "asset resolution",
            status="pass",
            detail=f"asset_id={resolved_id} ticker={resolved_ticker}",
        ))

    results.append(_timed(lambda: check_rag_hybrid_search_smoke(sb_factory, resolved_ticker or "PDUFA")))

    # Sub-agent stack + MCP modules
    results.append(_timed(check_sub_agent_stack))
    results.append(_timed(check_mcp_modules_importable))

    # Asset-specific (skip when unresolved)
    if resolved_id:
        asset_res, _ = check_asset_row(sb_factory, resolved_id)
        results.append(asset_res)
        results.append(_timed(lambda: check_stage1_fuel(sb_factory, resolved_id)))

    # Polygon, calibration, cassette dir, cost
    results.append(_timed(check_polygon_mode))
    results.append(_timed(lambda: check_active_calibration_curve(sb_factory)))
    results.append(_timed(check_cassette_dir_writable))
    results.append(_timed(check_cost_ceiling_visible))

    return results


def render_table(results: List[CheckResult], *, color: bool) -> str:
    rows = []
    name_w = max(len(r.name) for r in results) + 2
    for r in results:
        glyph = GLYPH.get(r.status, "?")
        if color:
            glyph = f"{COLOR.get(r.status, '')}{glyph}{COLOR['reset']}"
        line = f"  {glyph}  {r.name.ljust(name_w)} {r.detail}"
        if r.hint and r.status in {"fail", "warn"}:
            line += f"\n      → {r.hint}"
        rows.append(line)
    summary_counts = {s: sum(1 for r in results if r.status == s) for s in ("pass", "warn", "fail", "skip")}
    summary = (f"  pass={summary_counts['pass']}  warn={summary_counts['warn']}  "
               f"fail={summary_counts['fail']}  skip={summary_counts['skip']}")
    return "\n".join(rows) + "\n\n" + summary


def render_json(results: List[CheckResult]) -> str:
    return json.dumps([
        {
            "name": r.name, "status": r.status, "detail": r.detail,
            "hint": r.hint, "elapsed_ms": r.elapsed_ms, "blocking": r.blocking,
        }
        for r in results
    ], indent=2)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Gate 2B pre-flight verifier.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--asset-id", help="fda_assets.id UUID")
    g.add_argument("--ticker", help="ticker symbol (resolves to active asset)")
    p.add_argument("--json", action="store_true",
                   help="emit machine-readable JSON instead of a table")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s %(levelname)s: %(message)s")

    results = run_all(asset_id=args.asset_id, ticker=args.ticker)

    if args.json:
        print(render_json(results))
    else:
        is_tty = sys.stdout.isatty()
        print(render_table(results, color=is_tty))

    blocking_failures = [r for r in results if r.status == "fail" and r.blocking]
    return 0 if not blocking_failures else 1


if __name__ == "__main__":
    sys.exit(main())
