// =============================================================================
// bc-digest / index.test.ts — Phase 3 §8.2 idempotency + send tests (Deno).
//
// Run:  deno test --no-check --allow-env functions/bc-digest/index.test.ts
//
// FAKE Resend (a fetch stub) + FAKE Supabase client. NO live network / DB.
// Validates the send-loop contract via the exported runDigest():
//   - first invocation inserts a bc_digest_sends row + POSTs once per recipient;
//   - a second same-day invocation hits the UNIQUE -> 23505 -> recipient skipped,
//     NO second POST (asserted on the fake Resend call count);
//   - a non-2xx Resend => row status='failed' + the run closes 'partial';
//   - force=true bypasses dedup (manual resend);
//   - a thrown read error => the bc_pipeline_runs row still closes 'failed' (finally).
//   - the run closes with a CHECK-valid status {succeeded,partial,failed} (§8.4).
// =============================================================================

import { assert, assertEquals } from "https://deno.land/std@0.224.0/assert/mod.ts";

// runDigest imports the npm supabase-js TYPE only (createClient is used solely in
// Deno.serve, not in runDigest), so --no-check lets us import the module and drive
// runDigest with a structural fake. We must set the required env before import.
Deno.env.set("SUPABASE_URL", "https://example.test");
Deno.env.set("SUPABASE_SERVICE_ROLE_KEY", "test-service-key");
Deno.env.set("RESEND_API_KEY", "test-resend-key");
Deno.env.set("BC_DIGEST_DEV_RECIPIENTS", "");

const { runDigest } = await import("./index.ts");

const TODAY = "2026-06-03";

// ---- a flagged digest row (one in-window watchlist name) ------------------
function digestRow() {
  return {
    application_number: "761333",
    risk_band: "elevated",
    oof_percentile_rank: 78,
    appl_type: "BLA",
    pdufa_date: "2026-07-14",
    days_to_pdufa: 41,
    tier: "watchlist",
    materialized_at: TODAY + "T00:00:00Z",
    ticker: "PRTX",
    synthesis: {
      headline: "insider cluster 41d pre-PDUFA",
      what_changed: "two directors + CFO bought $2.1M",
      risk_vs_market: { model_risk_band: "elevated", model_percentile: 78, options_implied_move_pct: null, stance: "indeterminate_no_options" },
      drivers: [],
      bullets_up: [],
      bullets_down: [],
      risks: [],
      watch_items: ["8-K cadence into PDUFA"],
      recommended_action: "investigate",
      confidence: 0.66,
      provenance: { streams_available: { insider: true, options: false, news: true } },
    },
    trigger_reasons: ["insider_cluster"],
    fired_at: TODAY + "T14:05:00Z",
  };
}

// ---- Fake Supabase client (structural; supports the chained query builder) ----
// The supabase-js builder is thenable: `await sb.from(t).select().eq().eq()` and
// `await sb.from(t).select().eq().limit()` both resolve to {data,error}. We model
// that by making select()/eq()/limit() all return `this`, and `this` is awaitable
// via a then() that resolves the accumulated select. insert().select() and
// update().eq() are terminal calls that resolve immediately.
class FakeQuery {
  private _filters: Record<string, unknown> = {};
  private _op: string;
  private _insertRow: Record<string, unknown> | null = null;
  private _terminal: SelectResult | null = null;

  constructor(private table: string, private store: FakeStore, initialOp = "select") {
    this._op = initialOp;
  }

  select(_cols?: string) {
    if (this._op === "insert") {
      // insert(...).select("id") => terminal
      this._terminal = this.store.doInsert(this.table, this._insertRow!);
    }
    return this;
  }
  eq(col: string, val: unknown) { this._filters[col] = val; return this; }
  limit(_n: number) { return this; }
  insert(rowObj: Record<string, unknown>) { this._op = "insert"; this._insertRow = rowObj; return this; }
  update(rowObj: Record<string, unknown>) {
    return { eq: (col: string, val: unknown) => Promise.resolve(this.store.doUpdate(this.table, { [col]: val }, rowObj)) };
  }
  // make the builder awaitable
  then<T>(onFulfilled: (v: SelectResult) => T): T {
    const result = this._terminal ?? this.store.doSelect(this.table, this._filters);
    return onFulfilled(result);
  }
}

interface SelectResult { data: unknown; error: unknown; }

class FakeStore {
  pipelineRows: Record<string, Record<string, unknown>> = {};
  digestSends: Array<Record<string, unknown>> = [];
  pipelineCloses: Array<Record<string, unknown>> = [];
  rpcRows: unknown[] = [digestRow()];
  recipients: string[] = ["pedro@example.test"];
  config: Record<string, unknown> = {
    "l4.digest_flag_min_confidence": 0.6,
    "l4.digest_send_when_empty": true,
  };
  throwOnRpc = false;
  prefsQueries = 0; // counts notifications_prefs (v3 pool) reads — must stay 0 in you-only mode
  private _runSeq = 0;

  from(table: string) { return new FakeQuery(table, this, "select"); }

  // auth.admin.listUsers two-hop
  auth = {
    admin: {
      listUsers: (_: unknown) =>
        Promise.resolve({ data: { users: this.recipients.map((e, i) => ({ id: `u${i}`, email: e })) }, error: null }),
    },
  };

  async rpc(name: string, _args: unknown) {
    if (name === "bc_digest_rows") {
      if (this.throwOnRpc) return { data: null, error: { message: "boom", code: "P0001" } };
      return { data: this.rpcRows, error: null };
    }
    return { data: null, error: null };
  }

  doSelect(table: string, filters: Record<string, unknown>): SelectResult {
    if (table === "bc_config") {
      const key = filters["key"] as string;
      const v = this.config[key];
      return { data: v === undefined ? [] : [{ value: v }], error: null };
    }
    if (table === "notifications_prefs") {
      this.prefsQueries += 1;
      return { data: this.recipients.map((_, i) => ({ user_id: `u${i}` })), error: null };
    }
    return { data: [], error: null };
  }

  doInsert(table: string, rowObj: Record<string, unknown>): SelectResult {
    if (table === "bc_pipeline_runs") {
      const id = `run-${++this._runSeq}`;
      this.pipelineRows[id] = { ...rowObj, id };
      return { data: [{ id }], error: null };
    }
    if (table === "bc_digest_sends") {
      const dup = this.digestSends.find(
        (r) => r["digest_date"] === rowObj["digest_date"] && r["target"] === rowObj["target"],
      );
      if (dup) {
        return { data: null, error: { code: "23505", message: "duplicate key" } };
      }
      const id = `send-${this.digestSends.length + 1}`;
      const stored = { ...rowObj, id };
      this.digestSends.push(stored);
      return { data: [{ id }], error: null };
    }
    return { data: [], error: null };
  }

  doUpdate(table: string, filter: Record<string, unknown>, patch: Record<string, unknown>): SelectResult {
    if (table === "bc_pipeline_runs") {
      this.pipelineCloses.push(patch);
    }
    if (table === "bc_digest_sends") {
      const id = filter["id"];
      const r = this.digestSends.find((x) => x["id"] === id);
      if (r) Object.assign(r, patch);
    }
    return { data: null, error: null };
  }
}

// ---- Fake Resend (fetch stub) --------------------------------------------
function makeFakeResend(opts: { ok?: boolean } = {}) {
  const calls: Array<{ url: string; body: unknown }> = [];
  const fetchImpl = ((url: string | URL | Request, init?: RequestInit) => {
    calls.push({ url: String(url), body: init?.body ? JSON.parse(String(init.body)) : null });
    const ok = opts.ok ?? true;
    return Promise.resolve({
      ok,
      status: ok ? 200 : 422,
      json: () => Promise.resolve(ok ? { id: "resend-msg-1" } : { error: "invalid from" }),
    } as Response);
  }) as unknown as typeof fetch;
  return { fetchImpl, calls };
}

// ===========================================================================
// First send + same-day idempotency (no second POST)
// ===========================================================================
Deno.test("first invocation sends once; second same-day invocation skips (no second POST)", async () => {
  const store = new FakeStore();
  const resend1 = makeFakeResend();
  // deno-lint-ignore no-explicit-any
  const out1 = await runDigest(store as any, { today: TODAY, fetchImpl: resend1.fetchImpl });
  assertEquals(out1.emailed, 1);
  assertEquals(resend1.calls.length, 1, "exactly one Resend POST on first run");
  assertEquals(store.digestSends.length, 1);
  assertEquals(store.digestSends[0]["status"], "sent");

  // second run, SAME store + same day => 23505 on insert => skip, no POST
  const resend2 = makeFakeResend();
  // deno-lint-ignore no-explicit-any
  const out2 = await runDigest(store as any, { today: TODAY, fetchImpl: resend2.fetchImpl });
  assertEquals(resend2.calls.length, 0, "no second Resend POST for a same-day re-invocation");
  assertEquals(out2.skipped, 1);
  assertEquals(store.digestSends.length, 1, "no second bc_digest_sends row");
});

// ===========================================================================
// Non-2xx Resend => failed row + partial run (§8.2 / §8.4)
// ===========================================================================
Deno.test("non-2xx Resend => bc_digest_sends.status='failed' + run closes 'partial'", async () => {
  const store = new FakeStore();
  const resend = makeFakeResend({ ok: false });
  // deno-lint-ignore no-explicit-any
  const out = await runDigest(store as any, { today: TODAY, fetchImpl: resend.fetchImpl });
  assertEquals(out.failed, 1);
  assertEquals(store.digestSends[0]["status"], "failed");
  const close = store.pipelineCloses[store.pipelineCloses.length - 1];
  assertEquals(close["status"], "partial");
  assert(["succeeded", "partial", "failed"].includes(close["status"] as string), "CHECK-valid status");
});

// ===========================================================================
// force=true bypasses dedup (manual resend)
// ===========================================================================
Deno.test("force=true bypasses dedup => re-sends same day", async () => {
  const store = new FakeStore();
  const r1 = makeFakeResend();
  // deno-lint-ignore no-explicit-any
  await runDigest(store as any, { today: TODAY, fetchImpl: r1.fetchImpl });
  assertEquals(r1.calls.length, 1);

  const r2 = makeFakeResend();
  // deno-lint-ignore no-explicit-any
  const out = await runDigest(store as any, { today: TODAY, force: true, fetchImpl: r2.fetchImpl });
  assertEquals(r2.calls.length, 1, "force re-sends despite an existing same-day send row");
  assertEquals(out.emailed, 1);
});

// ===========================================================================
// Recipients: empty pool falls back to BC_DIGEST_DEV_RECIPIENTS
// ===========================================================================
Deno.test("empty recipient pool falls back to BC_DIGEST_DEV_RECIPIENTS", async () => {
  Deno.env.set("BC_DIGEST_DEV_RECIPIENTS", "dev1@example.test,dev2@example.test");
  // re-import to pick up the env (module-level const). Use a query string to bust cache.
  const mod = await import("./index.ts?devrecips");
  const store = new FakeStore();
  store.recipients = []; // no opt-in users
  const resend = makeFakeResend();
  // deno-lint-ignore no-explicit-any
  const out = await mod.runDigest(store as any, { today: TODAY, fetchImpl: resend.fetchImpl });
  assertEquals(out.emailed, 2, "both dev recipients receive the digest");
  assertEquals(resend.calls.length, 2);
  Deno.env.set("BC_DIGEST_DEV_RECIPIENTS", "");
});

// ===========================================================================
// Liveness/finally: a thrown read error still closes the run 'failed' (§8.2/§8.4)
// ===========================================================================
Deno.test("a thrown read error => bc_pipeline_runs row closes 'failed' with reason (finally)", async () => {
  const store = new FakeStore();
  store.throwOnRpc = true; // bc_digest_rows returns an error => runDigest throws
  const resend = makeFakeResend();
  let threw = false;
  try {
    // deno-lint-ignore no-explicit-any
    await runDigest(store as any, { today: TODAY, fetchImpl: resend.fetchImpl });
  } catch {
    threw = true;
  }
  assert(threw, "runDigest re-throws on a read error (send-or-throw)");
  const close = store.pipelineCloses[store.pipelineCloses.length - 1];
  assertEquals(close["status"], "failed");
  assert((close["reason"] as string).length > 0, "a failed close carries a reason");
  assertEquals(resend.calls.length, 0, "no email sent when the read failed");
});

// ===========================================================================
// Empty digest + send_when_empty=false => no send, run succeeds (§8.1/§1.0)
// ===========================================================================
Deno.test("0 flagged + send_when_empty=false => no send, run 'succeeded'", async () => {
  const store = new FakeStore();
  store.config["l4.digest_send_when_empty"] = false;
  // a watch-only row (monitor action => not flagged)
  const r = digestRow();
  r.synthesis.recommended_action = "monitor";
  r.synthesis.confidence = 0.3;
  store.rpcRows = [r];
  const resend = makeFakeResend();
  // deno-lint-ignore no-explicit-any
  const out = await runDigest(store as any, { today: TODAY, fetchImpl: resend.fetchImpl });
  assertEquals(out.emailed, 0);
  assertEquals(resend.calls.length, 0);
  const close = store.pipelineCloses[store.pipelineCloses.length - 1];
  assertEquals(close["status"], "succeeded");
});

// ===========================================================================
// Recipients precedence (conan port): allowlist + you-only NEVER touch v3 prefs
// ===========================================================================
Deno.test("l4.digest_recipient_email allowlist wins; notifications_prefs NOT queried", async () => {
  const store = new FakeStore();
  store.config["l4.digest_recipient_email"] = ["only-you@example.test"];
  store.recipients = ["v3user@example.test"]; // would leak via prefs if consulted
  const resend = makeFakeResend();
  // deno-lint-ignore no-explicit-any
  const out = await runDigest(store as any, { today: TODAY, fetchImpl: resend.fetchImpl });
  assertEquals(out.emailed, 1);
  assertEquals(resend.calls.length, 1);
  // deno-lint-ignore no-explicit-any
  assertEquals((resend.calls[0].body as any).to[0], "only-you@example.test");
  assertEquals(store.prefsQueries, 0, "v3 notifications_prefs must NOT be queried when the bc allowlist is set");
});

Deno.test("BC_DIGEST_DEV_RECIPIENTS (you-only warm-up) wins over v3 prefs; prefs NOT queried", async () => {
  Deno.env.set("BC_DIGEST_DEV_RECIPIENTS", "you@example.test");
  const mod = await import("./index.ts?devonly"); // fresh module reads the env
  const store = new FakeStore();
  store.recipients = ["v3user@example.test"]; // would leak via prefs if consulted
  const resend = makeFakeResend();
  // deno-lint-ignore no-explicit-any
  const out = await mod.runDigest(store as any, { today: TODAY, fetchImpl: resend.fetchImpl });
  assertEquals(out.emailed, 1);
  // deno-lint-ignore no-explicit-any
  assertEquals((resend.calls[0].body as any).to[0], "you@example.test");
  assertEquals(store.prefsQueries, 0, "v3 prefs must NOT be queried when DEV recipients are set");
  Deno.env.set("BC_DIGEST_DEV_RECIPIENTS", "");
});

// ===========================================================================
// Inbound auth gate (conan port): wrong/missing x-service-key => 401
// ===========================================================================
Deno.test("handleRequest rejects missing/wrong x-service-key with 401", async () => {
  Deno.env.set("BC_DIGEST_TRIGGER_KEY", "trig-test"); // use the env override (no DB fetch)
  const mod = await import("./index.ts?auth");
  const r1 = await mod.handleRequest(new Request("https://x.test/", { method: "POST" }));
  assertEquals(r1.status, 401); // missing header
  const r2 = await mod.handleRequest(
    new Request("https://x.test/", { method: "POST", headers: { "x-service-key": "wrong" } }),
  );
  assertEquals(r2.status, 401); // wrong secret
  Deno.env.delete("BC_DIGEST_TRIGGER_KEY");
});
