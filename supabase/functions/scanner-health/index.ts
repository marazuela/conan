// Scanner-health read endpoint — Phase 1 §11 task 12.
//
// GET /functions/v1/scanner-health
//
// Read-only JSON snapshot of the 17-scanner fleet + the top operator_flags
// (spec §3.4 + §7.6). Intended as the data source for the dashboard's scanner-
// health card and for curl-based smoke checks from Pedro's laptop.
//
// Auth: JWT-verified. Service role bypass for internal health checks via a
// header `x-service-key` that matches SUPABASE_SERVICE_ROLE_KEY — lets Modal
// dispatchers hit the endpoint without a user token.
//
// Response shape:
//   {
//     generated_at: ISO-8601,
//     summary: {
//       scanners: {
//         total, green, yellow, red,
//         by_status,
//         by_state_label,
//       },
//       open_flags: { critical, warn, info }
//     },
//     scanners: [
//       {
//         name, status, cadence, geography, default_scoring_profile,
//         timeout_soft_s, timeout_hard_s,
//         last_run_utc, last_run_status, last_run_signals,
//         last_probe_at, last_probe_status, last_probe_latency_ms,
//         recent_runs: [{status, signals_emitted, started_at, completed_at, elapsed_s, error?}, ... up to 5],
//         health, state_label, state_reason,
//         has_running_run, running_run_count, is_stale,
//         run_age_minutes, probe_age_minutes,
//         latest_run_status, latest_run_started_at, latest_run_completed_at
//       }, ...
//     ],
//     open_flags: [
//       { id, severity, source, kind, scanner_name?, title, age_minutes, created_at }, ...
//     ]
//   }

import { createClient } from "npm:@supabase/supabase-js@2";
import {
  deriveScannerState,
  type Health,
  type OperatorFlagSnapshot,
  type ScannerRunSnapshot,
} from "./state.ts";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

const sb = createClient(SUPABASE_URL, SERVICE_KEY, {
  auth: { autoRefreshToken: false, persistSession: false },
});

interface ScannerRow {
  id: string;
  name: string;
  status: string;
  cadence: string;
  geography: string | null;
  default_scoring_profile: string;
  timeout_soft_s: number;
  timeout_hard_s: number;
  last_run_utc: string | null;
  last_run_status: string | null;
  last_run_signals: number | null;
  last_probe_at: string | null;
  last_probe_status: string | null;
  last_probe_latency_ms: number | null;
}

interface ScannerRunRow {
  scanner_id: string;
  status: string;
  signals_emitted: number;
  started_at: string;
  completed_at: string | null;
  errors: Array<Record<string, unknown>>;
  warnings?: string[] | null;
  run_metrics?: Record<string, unknown> | null;
}

interface ScannerRunDiagnostics {
  error: string | null;
  warnings: string[];
  metrics: Record<string, unknown> | null;
}

interface OperatorFlagRow {
  id: string;
  severity: string;
  source: string;
  kind: string;
  title: string;
  scanner_id: string | null;
  created_at: string;
}

Deno.serve(async (req: Request) => {
  // Service-role header bypass (for Modal dispatchers + curl smoke tests).
  const svcHeader = req.headers.get("x-service-key");
  const isServiceBypass = svcHeader && svcHeader === SERVICE_KEY;

  // Otherwise require a verified JWT (Supabase Auth users).
  if (!isServiceBypass) {
    const authz = req.headers.get("authorization") ?? "";
    if (!authz.toLowerCase().startsWith("bearer ")) {
      return new Response("unauthorized", { status: 401 });
    }
    const token = authz.slice(7);
    const { data: userRes, error: userErr } = await sb.auth.getUser(token);
    if (userErr || !userRes.user) return new Response("unauthorized", { status: 401 });
  }

  try {
    const out = await build();
    return new Response(JSON.stringify(out), {
      status: 200,
      headers: { "content-type": "application/json", "cache-control": "no-store" },
    });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return new Response(JSON.stringify({ error: message }), {
      status: 500,
      headers: { "content-type": "application/json" },
    });
  }
});

async function build() {
  // --- 1. Fetch scanners + recent runs + open operator_flags in parallel.
  const [scannersRes, runsRes, flagsRes] = await Promise.all([
    sb.from("scanners").select(
      "id,name,status,cadence,geography,default_scoring_profile,timeout_soft_s,timeout_hard_s," +
        "last_run_utc,last_run_status,last_run_signals," +
        "last_probe_at,last_probe_status,last_probe_latency_ms",
    ).order("name"),
    // Last 7 days of scanner_runs for the per-scanner "recent_runs" rolling view.
    sb.from("scanner_runs").select(
      "scanner_id,status,signals_emitted,started_at,completed_at,errors,warnings,run_metrics",
    ).gte("started_at", new Date(Date.now() - 7 * 24 * 60 * 60 * 1000).toISOString())
      .order("started_at", { ascending: false }).limit(500),
    sb.from("operator_flags").select(
      "id,severity,source,kind,title,scanner_id,created_at",
    ).is("resolved_at", null).order("severity", { ascending: false })
      .order("created_at", { ascending: false }).limit(50),
  ]);
  if (scannersRes.error) throw scannersRes.error;
  if (runsRes.error) throw runsRes.error;
  if (flagsRes.error) throw flagsRes.error;
  const scanners = (scannersRes.data ?? []) as unknown as ScannerRow[];
  const runs = (runsRes.data ?? []) as unknown as ScannerRunRow[];
  const flags = (flagsRes.data ?? []) as unknown as OperatorFlagRow[];

  // --- 2. Index runs by scanner_id; keep first 5 (already sorted DESC).
  const runsByScanner = new Map<string, ScannerRunRow[]>();
  for (const r of runs) {
    const arr = runsByScanner.get(r.scanner_id) ?? [];
    if (arr.length < 5) arr.push(r);
    runsByScanner.set(r.scanner_id, arr);
  }

  // --- 3. Index flags by scanner_id for the per-scanner health roll-up.
  const flagsByScanner = new Map<string, OperatorFlagRow[]>();
  for (const f of flags) {
    if (!f.scanner_id) continue;
    const arr = flagsByScanner.get(f.scanner_id) ?? [];
    arr.push(f);
    flagsByScanner.set(f.scanner_id, arr);
  }

  // --- 4. Scanner ID → name for the open-flags list enrichment.
  const nameById = new Map(scanners.map((s) => [s.id, s.name]));

  // --- 5. Build per-scanner entries with derived health color.
  const scannerOut = scanners.map((s) => {
    const recent = (runsByScanner.get(s.id) ?? []).map((r) => {
      const diagnostics = extractRunDiagnostics(r.errors);
      const warnings = Array.isArray(r.warnings) && r.warnings.length > 0
        ? r.warnings.filter((value): value is string => typeof value === "string")
        : diagnostics.warnings;
      const metrics = r.run_metrics && typeof r.run_metrics === "object" &&
          !Array.isArray(r.run_metrics) && Object.keys(r.run_metrics).length > 0
        ? r.run_metrics
        : diagnostics.metrics;
      return {
      status: r.status,
      signals_emitted: r.signals_emitted,
      started_at: r.started_at,
      completed_at: r.completed_at,
      elapsed_s: r.completed_at
        ? Math.round((Date.parse(r.completed_at) - Date.parse(r.started_at)) / 1000)
        : null,
      error: diagnostics.error,
      warnings,
      metrics,
    };
    });
    const myFlags = flagsByScanner.get(s.id) ?? [];
    const derived = deriveScannerState(
      s,
      recent as ScannerRunSnapshot[],
      myFlags as OperatorFlagSnapshot[],
      new Date(),
    );
    return {
      name: s.name,
      status: s.status,
      cadence: s.cadence,
      geography: s.geography,
      default_scoring_profile: s.default_scoring_profile,
      timeout_soft_s: s.timeout_soft_s,
      timeout_hard_s: s.timeout_hard_s,
      last_run_utc: s.last_run_utc,
      last_run_status: s.last_run_status,
      last_run_signals: s.last_run_signals,
      last_probe_at: s.last_probe_at,
      last_probe_status: s.last_probe_status,
      last_probe_latency_ms: s.last_probe_latency_ms,
      recent_runs: recent,
      open_flag_count: myFlags.length,
      latest_run_metrics: recent[0]?.metrics ?? null,
      latest_run_warnings: recent[0]?.warnings ?? [],
      ...derived,
    };
  });

  // --- 6. Roll up fleet + flag summary.
  const summary = {
    scanners: {
      total: scannerOut.length,
      green: scannerOut.filter((s) => s.health === "green").length,
      yellow: scannerOut.filter((s) => s.health === "yellow").length,
      red: scannerOut.filter((s) => s.health === "red").length,
      by_status: summarizeBy(scannerOut, (s) => s.status),
      by_state_label: summarizeBy(scannerOut, (s) => s.state_label),
    },
    open_flags: {
      critical: flags.filter((f) => f.severity === "critical").length,
      // Kept for backward compatibility with historical snapshots. New
      // operator_flags rows use the DB-enforced critical/warn/info contract.
      error: flags.filter((f) => f.severity === "error").length,
      warn: flags.filter((f) => f.severity === "warn").length,
      info: flags.filter((f) => f.severity === "info").length,
    },
  };

  const now = Date.now();
  const flagsOut = flags.map((f) => ({
    id: f.id,
    severity: f.severity,
    source: f.source,
    kind: f.kind,
    scanner_name: f.scanner_id ? nameById.get(f.scanner_id) ?? null : null,
    title: f.title,
    age_minutes: Math.round((now - Date.parse(f.created_at)) / 60000),
    created_at: f.created_at,
  }));

  return {
    generated_at: new Date().toISOString(),
    summary,
    scanners: scannerOut,
    open_flags: flagsOut,
  };
}

function summarizeError(errors: Array<Record<string, unknown>> | null | undefined): string | null {
  return extractRunDiagnostics(errors).error;
}

function extractRunDiagnostics(
  errors: Array<Record<string, unknown>> | null | undefined,
): ScannerRunDiagnostics {
  if (!errors || errors.length === 0) {
    return { error: null, warnings: [], metrics: null };
  }
  const warnings: string[] = [];
  let metrics: Record<string, unknown> | null = null;
  let error: string | null = null;

  for (const entry of errors) {
    if (!entry || typeof entry !== "object") continue;
    const maybeWarnings = entry["warnings"];
    if (Array.isArray(maybeWarnings)) {
      warnings.push(...maybeWarnings.filter((value): value is string => typeof value === "string"));
    }
    const maybeMetrics = entry["metrics"];
    if (maybeMetrics && typeof maybeMetrics === "object" && !Array.isArray(maybeMetrics)) {
      metrics = maybeMetrics as Record<string, unknown>;
    }
    if (!error) {
      const typed = entry as { type?: string; message?: string; error?: string };
      if (typed?.message) {
        error = typeof typed.type === "string" ? `${typed.type}: ${typed.message}` : typed.message;
      } else if (typeof typed?.error === "string") {
        error = typed.error;
      }
    }
  }

  if (!error) {
    const firstWithError = errors.find((entry) =>
      Boolean(entry && typeof entry === "object" && ("error" in entry || "message" in entry))
    );
    if (firstWithError) {
      error = JSON.stringify(firstWithError).slice(0, 200);
    }
  }

  return { error, warnings, metrics };
}

function summarizeBy<T>(rows: T[], key: (r: T) => string): Record<string, number> {
  const out: Record<string, number> = {};
  for (const r of rows) {
    const k = key(r);
    out[k] = (out[k] ?? 0) + 1;
  }
  return out;
}
