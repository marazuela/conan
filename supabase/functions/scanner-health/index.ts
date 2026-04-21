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
//       scanners: { total, operational, degraded, disabled, auth_required },
//       open_flags: { critical, warn, info }
//     },
//     scanners: [
//       {
//         name, status, cadence, geography,
//         timeout_soft_s, timeout_hard_s,
//         last_run_utc, last_run_status, last_run_signals,
//         last_probe_at, last_probe_status, last_probe_latency_ms,
//         recent_runs: [{status, signals_emitted, started_at, completed_at, elapsed_s, error?}, ... up to 5],
//         health: "green" | "yellow" | "red"   // derived below
//       }, ...
//     ],
//     open_flags: [
//       { id, severity, source, kind, scanner_name?, title, age_minutes, created_at }, ...
//     ]
//   }

import { createClient } from "npm:@supabase/supabase-js@2";

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
      "id,name,status,cadence,geography,timeout_soft_s,timeout_hard_s," +
        "last_run_utc,last_run_status,last_run_signals," +
        "last_probe_at,last_probe_status,last_probe_latency_ms",
    ).order("name"),
    // Last 7 days of scanner_runs for the per-scanner "recent_runs" rolling view.
    sb.from("scanner_runs").select(
      "scanner_id,status,signals_emitted,started_at,completed_at,errors",
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
    const recent = (runsByScanner.get(s.id) ?? []).map((r) => ({
      status: r.status,
      signals_emitted: r.signals_emitted,
      started_at: r.started_at,
      completed_at: r.completed_at,
      elapsed_s: r.completed_at
        ? Math.round((Date.parse(r.completed_at) - Date.parse(r.started_at)) / 1000)
        : null,
      error: summarizeError(r.errors),
    }));
    const myFlags = flagsByScanner.get(s.id) ?? [];
    const health = deriveHealth(s, recent, myFlags);
    return {
      name: s.name,
      status: s.status,
      cadence: s.cadence,
      geography: s.geography,
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
      health,
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
    },
    open_flags: {
      critical: flags.filter((f) => f.severity === "critical").length,
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

// ---------------------------------------------------------------------------
// Health derivation (simple rule: aggregate signals, not a magic threshold)
// ---------------------------------------------------------------------------

type Health = "green" | "yellow" | "red";

function deriveHealth(
  s: ScannerRow,
  recent: Array<{ status: string }>,
  flags: OperatorFlagRow[],
): Health {
  // Critical open flag → red.
  if (flags.some((f) => f.severity === "critical")) return "red";
  // Scanner row explicitly disabled or errored → red.
  if (s.status === "deprecated") return "red";
  // Last run error or timeout → red (one-shot failure demands attention).
  if (s.last_run_status === "error" || s.last_run_status === "timeout") return "red";
  // auth_required + persistent → yellow (documented deferral in v2 for courtlistener/kind).
  if (s.last_run_status === "auth_required") return "yellow";
  // Probe drifted → yellow.
  if (s.last_probe_status && s.last_probe_status !== "ok") return "yellow";
  // 2+ partials in the last 5 runs → yellow (budget getting tight).
  const partials = recent.filter((r) => r.status === "partial").length;
  if (partials >= 2) return "yellow";
  // Any warn flag → yellow.
  if (flags.some((f) => f.severity === "warn")) return "yellow";
  // No runs ever → yellow (not red — might be planned/not yet cadenced).
  if (!s.last_run_utc) return "yellow";
  // Last run ok and everything else clean → green.
  return "green";
}

function summarizeError(errors: Array<Record<string, unknown>> | null | undefined): string | null {
  if (!errors || errors.length === 0) return null;
  const first = errors[0] as { type?: string; message?: string };
  if (first?.message) {
    return typeof first.type === "string" ? `${first.type}: ${first.message}` : first.message;
  }
  return JSON.stringify(first).slice(0, 200);
}

function summarizeBy<T>(rows: T[], key: (r: T) => string): Record<string, number> {
  const out: Record<string, number> = {};
  for (const r of rows) {
    const k = key(r);
    out[k] = (out[k] ?? 0) + 1;
  }
  return out;
}
