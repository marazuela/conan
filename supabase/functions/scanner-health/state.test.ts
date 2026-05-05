import { deriveScannerState } from "./state.ts";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

Deno.test("running beats idle when a live run exists", () => {
  const state = deriveScannerState(
    {
      status: "operational",
      cadence: "daily",
      last_run_utc: null,
      last_run_status: null,
      last_probe_at: null,
      last_probe_status: null,
    },
    [{ status: "running", started_at: "2026-04-21T09:00:00Z", completed_at: null }],
    [],
    new Date("2026-04-21T09:10:00Z"),
  );

  assert(state.state_label === "running", "expected running label");
  assert(state.has_running_run === true, "expected running flag");
  assert(state.running_run_count === 1, "expected running count");
});

Deno.test("stale respects two-times cadence threshold", () => {
  const state = deriveScannerState(
    {
      status: "operational",
      cadence: "3h",
      last_run_utc: "2026-04-21T02:00:00Z",
      last_run_status: "ok",
      last_probe_at: "2026-04-21T07:30:00Z",
      last_probe_status: "ok",
    },
    [],
    [],
    new Date("2026-04-21T08:10:00Z"),
  );

  assert(state.state_label === "stale", "expected stale label");
  assert(state.is_stale === true, "expected stale flag");
});

Deno.test("critical flags escalate the state to error", () => {
  const state = deriveScannerState(
    {
      status: "operational",
      cadence: "daily",
      last_run_utc: "2026-04-21T08:00:00Z",
      last_run_status: "ok",
      last_probe_at: "2026-04-21T08:05:00Z",
      last_probe_status: "drift",
    },
    [],
    [{ severity: "critical", title: "endpoint drift" }],
    new Date("2026-04-21T08:10:00Z"),
  );

  assert(state.state_label === "error", "expected critical flag to force error");
  assert(state.health === "red", "expected red health");
  assert(
    state.state_reason.includes("endpoint drift"),
    "expected critical flag title in the reason",
  );
});

Deno.test("error severity flags also escalate to red", () => {
  const state = deriveScannerState(
    {
      status: "operational",
      cadence: "daily",
      last_run_utc: "2026-04-21T08:00:00Z",
      last_run_status: "ok",
      last_probe_at: "2026-04-21T08:05:00Z",
      last_probe_status: "ok",
    },
    [],
    [{ severity: "error", title: "thesis_jobs DLQ surge", source: "thesis_jobs" }],
    new Date("2026-04-21T08:10:00Z"),
  );

  assert(state.state_label === "error", "error-severity flag should force error label");
  assert(state.health === "red", "error severity should be red");
  assert(
    state.state_reason.includes("thesis_jobs DLQ surge"),
    "expected error flag title in the reason",
  );
});

Deno.test("critical wins over error when both are present", () => {
  const state = deriveScannerState(
    {
      status: "operational",
      cadence: "daily",
      last_run_utc: "2026-04-21T08:00:00Z",
      last_run_status: "ok",
      last_probe_at: "2026-04-21T08:05:00Z",
      last_probe_status: "ok",
    },
    [],
    [
      { severity: "error", title: "thesis_jobs DLQ surge" },
      { severity: "critical", title: "endpoint drift" },
    ],
    new Date("2026-04-21T08:10:00Z"),
  );

  assert(state.health === "red", "expected red health");
  assert(
    state.state_reason.includes("endpoint drift"),
    "critical flag title should win when both severities present",
  );
});

Deno.test("warn flags keep an ok run yellow without changing the label", () => {
  const state = deriveScannerState(
    {
      status: "operational",
      cadence: "daily",
      last_run_utc: "2026-04-21T08:00:00Z",
      last_run_status: "ok",
      last_probe_at: "2026-04-21T08:05:00Z",
      last_probe_status: "ok",
    },
    [],
    [{ severity: "warn", title: "fallback endpoint active" }],
    new Date("2026-04-21T08:10:00Z"),
  );

  assert(state.state_label === "ok", "warn flags should not rewrite ok into idle");
  assert(state.health === "yellow", "warn flags should yellow the health");
});

Deno.test("latest observed timeout overrides stale scanners row metadata", () => {
  const state = deriveScannerState(
    {
      status: "operational",
      cadence: "3h",
      last_run_utc: "2026-04-21T06:00:00Z",
      last_run_status: "ok",
      last_probe_at: null,
      last_probe_status: null,
    },
    [{
      status: "timeout",
      started_at: "2026-04-21T08:55:00Z",
      completed_at: "2026-04-21T09:15:00Z",
    }],
    [],
    new Date("2026-04-21T09:16:00Z"),
  );

  assert(state.state_label === "timeout", "expected latest timeout to win");
  assert(state.latest_run_status === "timeout", "expected latest observed status");
});

Deno.test("paused scanners render as disabled", () => {
  const state = deriveScannerState(
    {
      status: "paused",
      cadence: "daily",
      last_run_utc: "2026-04-21T08:00:00Z",
      last_run_status: "ok",
      last_probe_at: "2026-04-21T08:05:00Z",
      last_probe_status: "ok",
    },
    [],
    [],
    new Date("2026-04-21T08:10:00Z"),
  );

  assert(state.state_label === "disabled", "paused scanners should render as disabled");
  assert(state.health === "yellow", "paused scanners should remain operator-visible");
  assert(
    state.state_reason.includes("paused"),
    "expected paused status to surface in the reason",
  );
});

Deno.test("partial state surfaces structured partial reason when present", () => {
  const state = deriveScannerState(
    {
      status: "operational",
      cadence: "3h",
      last_run_utc: "2026-04-21T08:00:00Z",
      last_run_status: "partial",
      last_probe_at: null,
      last_probe_status: null,
    },
    [{
      status: "partial",
      started_at: "2026-04-21T08:00:00Z",
      completed_at: "2026-04-21T08:00:20Z",
      warnings: ["budget exhausted"],
      metrics: { partial_reasons: ["budget_exhausted_keyword_phase"] },
    }],
    [],
    new Date("2026-04-21T08:10:00Z"),
  );

  assert(
    state.state_reason.includes("budget_exhausted_keyword_phase"),
    "expected structured partial reason to surface",
  );
});
