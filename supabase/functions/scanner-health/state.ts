export type Health = "green" | "yellow" | "red";

export type ScannerStateLabel =
  | "running"
  | "stale"
  | "error"
  | "timeout"
  | "partial"
  | "auth_required"
  | "ok"
  | "idle"
  | "disabled";

export interface ScannerStateInput {
  status: string;
  cadence: string;
  last_run_utc: string | null;
  last_run_status: string | null;
  last_probe_at: string | null;
  last_probe_status: string | null;
}

export interface ScannerRunSnapshot {
  status: string;
  started_at: string;
  completed_at: string | null;
  warnings?: string[];
  metrics?: Record<string, unknown> | null;
}

export interface OperatorFlagSnapshot {
  severity: string;
  source?: string;
  kind?: string;
  title?: string;
}

export interface DerivedScannerState {
  health: Health;
  state_label: ScannerStateLabel;
  state_reason: string;
  has_running_run: boolean;
  running_run_count: number;
  is_stale: boolean;
  run_age_minutes: number | null;
  probe_age_minutes: number | null;
  latest_run_status: string | null;
  latest_run_started_at: string | null;
  latest_run_completed_at: string | null;
}

const MINUTES_PER_HOUR = 60;
const MS_PER_MINUTE = 60_000;

function parseMs(value: string | null): number | null {
  if (!value) return null;
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? null : parsed;
}

function ageMinutes(value: string | null, nowMs: number): number | null {
  const parsed = parseMs(value);
  if (parsed === null) return null;
  return Math.max(Math.round((nowMs - parsed) / MS_PER_MINUTE), 0);
}

function formatAge(minutes: number | null): string {
  if (minutes === null) return "unknown age";
  if (minutes < MINUTES_PER_HOUR) return `${minutes}m ago`;
  const hours = minutes / MINUTES_PER_HOUR;
  if (hours < 48) return `${hours.toFixed(1).replace(/\.0$/, "")}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

function summarizeFlag(flag: OperatorFlagSnapshot | undefined): string | null {
  if (!flag) return null;
  return flag.title || flag.kind || flag.source || null;
}

function partialReason(run: ScannerRunSnapshot | null): string | null {
  const metrics = run?.metrics;
  const reasons = metrics?.["partial_reasons"];
  if (Array.isArray(reasons)) {
    const first = reasons.find((value): value is string => typeof value === "string");
    if (first) return first;
  }
  const warning = run?.warnings?.find((value) => typeof value === "string");
  return warning ?? null;
}

function effectiveLatestRun(
  input: ScannerStateInput,
  recentRuns: ScannerRunSnapshot[],
): ScannerRunSnapshot | null {
  const latestRun = recentRuns[0] ?? null;
  if (!latestRun) return null;

  const latestObservedAt = parseMs(latestRun.completed_at) ?? parseMs(latestRun.started_at);
  const lastRunAt = parseMs(input.last_run_utc);
  if (lastRunAt === null) return latestRun;
  if (latestObservedAt !== null && latestObservedAt >= lastRunAt) return latestRun;
  return null;
}

export function cadenceMs(cadence: string): number | null {
  switch (cadence) {
    case "3h":
      return 3 * MINUTES_PER_HOUR * MS_PER_MINUTE;
    case "daily":
      return 24 * MINUTES_PER_HOUR * MS_PER_MINUTE;
    case "weekly":
      return 7 * 24 * MINUTES_PER_HOUR * MS_PER_MINUTE;
    default:
      return null;
  }
}

function stateResponse(
  state_label: ScannerStateLabel,
  state_reason: string,
  health: Health,
  extras: Omit<DerivedScannerState, "health" | "state_label" | "state_reason">,
): DerivedScannerState {
  return {
    state_label,
    state_reason,
    health,
    ...extras,
  };
}

export function deriveScannerState(
  input: ScannerStateInput,
  recentRuns: ScannerRunSnapshot[],
  flags: OperatorFlagSnapshot[],
  now = new Date(),
): DerivedScannerState {
  const nowMs = now.getTime();
  const runAgeMinutes = ageMinutes(input.last_run_utc, nowMs);
  const probeAgeMinutes = ageMinutes(input.last_probe_at, nowMs);
  const cadenceWindowMs = cadenceMs(input.cadence);
  const isStale =
    cadenceWindowMs !== null &&
    input.last_run_utc !== null &&
    (runAgeMinutes ?? 0) * MS_PER_MINUTE > cadenceWindowMs * 2;

  const runningRunCount = recentRuns.filter((run) => run.status === "running").length;
  const hasRunningRun = runningRunCount > 0;
  const latestObservedRun = effectiveLatestRun(input, recentRuns);
  const effectiveStatus = latestObservedRun?.status ?? input.last_run_status;
  const criticalFlag = flags.find((flag) => flag.severity === "critical");
  const warnFlag = flags.find((flag) => flag.severity === "warn");
  const probeIssue = input.last_probe_status && input.last_probe_status !== "ok";

  const extras = {
    has_running_run: hasRunningRun,
    running_run_count: runningRunCount,
    is_stale: isStale,
    run_age_minutes: runAgeMinutes,
    probe_age_minutes: probeAgeMinutes,
    latest_run_status: latestObservedRun?.status ?? input.last_run_status,
    latest_run_started_at: latestObservedRun?.started_at ?? null,
    latest_run_completed_at: latestObservedRun?.completed_at ?? null,
  };

  if (input.status !== "operational") {
    return stateResponse(
      "disabled",
      `registry status=${input.status}`,
      input.status === "deprecated" ? "red" : "yellow",
      extras,
    );
  }

  if (criticalFlag) {
    return stateResponse(
      "error",
      `critical flag: ${summarizeFlag(criticalFlag) ?? "operator attention required"}`,
      "red",
      extras,
    );
  }

  if (effectiveStatus === "error") {
    return stateResponse(
      "error",
      latestObservedRun
        ? `latest run failed (${formatAge(ageMinutes(latestObservedRun.started_at, nowMs))})`
        : "last run failed",
      "red",
      extras,
    );
  }

  if (effectiveStatus === "timeout") {
    return stateResponse(
      "timeout",
      latestObservedRun
        ? `latest run timed out (${formatAge(ageMinutes(latestObservedRun.started_at, nowMs))})`
        : "last run timed out",
      "red",
      extras,
    );
  }

  if (hasRunningRun) {
    return stateResponse(
      "running",
      `${runningRunCount} active run${runningRunCount === 1 ? "" : "s"} in progress`,
      "yellow",
      extras,
    );
  }

  if (effectiveStatus === "auth_required") {
    return stateResponse(
      "auth_required",
      "awaiting credentials or upstream auth",
      "yellow",
      extras,
    );
  }

  if (effectiveStatus === "partial") {
    const detail = partialReason(latestObservedRun);
    return stateResponse(
      "partial",
      detail ? `latest run partial: ${detail}` : "latest run completed with warnings or dropped records",
      "yellow",
      extras,
    );
  }

  if (isStale) {
    return stateResponse(
      "stale",
      `last run ${formatAge(runAgeMinutes)} (>2x cadence)`,
      "yellow",
      extras,
    );
  }

  if (effectiveStatus === "ok") {
    if (warnFlag) {
      return stateResponse(
        "ok",
        `warn flag: ${summarizeFlag(warnFlag) ?? "operator attention advised"}`,
        "yellow",
        extras,
      );
    }
    if (probeIssue) {
      return stateResponse(
        "ok",
        `probe status=${input.last_probe_status} (${formatAge(probeAgeMinutes)})`,
        "yellow",
        extras,
      );
    }
    return stateResponse(
      "ok",
      input.last_run_utc ? `last run ${formatAge(runAgeMinutes)}` : "healthy",
      "green",
      extras,
    );
  }

  if (warnFlag) {
    return stateResponse(
      "idle",
      `warn flag: ${summarizeFlag(warnFlag) ?? "operator attention advised"}`,
      "yellow",
      extras,
    );
  }

  if (probeIssue) {
    return stateResponse(
      "idle",
      `probe status=${input.last_probe_status} (${formatAge(probeAgeMinutes)})`,
      "yellow",
      extras,
    );
  }

  return stateResponse(
    "idle",
    "no completed runs recorded",
    "yellow",
    extras,
  );
}
