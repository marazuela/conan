// F-203: rubric_apply_caps retry helper. Audit ID F-203 in
// audit/findings_2026-04-27.md. Exercises the exponential-backoff retry
// behaviour around transient Modal failures so we don't DLQ a recoverable
// fetch on the first hiccup.

import { fetchWithRetry } from "./fetch-retry.ts";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

interface FetchCall {
  url: string;
  init: RequestInit;
}

function stubFetch(responses: Array<Response | Error>): {
  calls: FetchCall[];
  restore: () => void;
} {
  const original = globalThis.fetch;
  const calls: FetchCall[] = [];
  let i = 0;
  globalThis.fetch = ((url: string | URL | Request, init?: RequestInit) => {
    calls.push({ url: String(url), init: init ?? {} });
    const next = responses[Math.min(i, responses.length - 1)];
    i += 1;
    if (next instanceof Error) return Promise.reject(next);
    return Promise.resolve(next);
  }) as typeof globalThis.fetch;
  return {
    calls,
    restore: () => {
      globalThis.fetch = original;
    },
  };
}

const noSleep = (_ms: number) => Promise.resolve();

Deno.test("fetchWithRetry returns 200 after two transient 503s", async () => {
  const { calls, restore } = stubFetch([
    new Response("svc", { status: 503 }),
    new Response("svc", { status: 503 }),
    new Response('{"ok":1}', { status: 200 }),
  ]);
  try {
    const r = await fetchWithRetry(
      "https://example.test",
      { method: "POST" },
      [10, 10, 10],
      noSleep,
    );
    assert(r.status === 200, `expected 200, got ${r.status}`);
    assert(calls.length === 3, `expected 3 fetches, got ${calls.length}`);
  } finally {
    restore();
  }
});

Deno.test("fetchWithRetry throws after exhausting retries on persistent 503", async () => {
  const { calls, restore } = stubFetch([
    new Response("svc", { status: 503 }),
    new Response("svc", { status: 503 }),
    new Response("svc", { status: 503 }),
    new Response("svc", { status: 503 }),
  ]);
  try {
    let threw = false;
    try {
      await fetchWithRetry(
        "https://example.test",
        { method: "POST" },
        [10, 10, 10],
        noSleep,
      );
    } catch (err) {
      threw = true;
      assert(
        err instanceof Error && err.message.includes("503"),
        `error should reference 503 status, got: ${err}`,
      );
    }
    assert(threw, "fetchWithRetry should throw after exhausting retries");
    // 1 initial attempt + 3 retries = 4 total fetches.
    assert(calls.length === 4, `expected 4 fetches, got ${calls.length}`);
  } finally {
    restore();
  }
});

Deno.test("fetchWithRetry does NOT retry on non-retryable 4xx (returns response)", async () => {
  const { calls, restore } = stubFetch([
    new Response("bad input", { status: 400 }),
  ]);
  try {
    const r = await fetchWithRetry(
      "https://example.test",
      { method: "POST" },
      [10, 10, 10],
      noSleep,
    );
    assert(r.status === 400, `expected 400 passthrough, got ${r.status}`);
    assert(
      calls.length === 1,
      `expected 1 fetch (no retry on 400), got ${calls.length}`,
    );
  } finally {
    restore();
  }
});

Deno.test("fetchWithRetry retries on 429 rate-limit", async () => {
  const { calls, restore } = stubFetch([
    new Response("slow down", { status: 429 }),
    new Response('{"ok":1}', { status: 200 }),
  ]);
  try {
    const r = await fetchWithRetry(
      "https://example.test",
      { method: "POST" },
      [10, 10, 10],
      noSleep,
    );
    assert(r.status === 200, `expected eventual 200, got ${r.status}`);
    assert(calls.length === 2, `expected 2 fetches, got ${calls.length}`);
  } finally {
    restore();
  }
});

Deno.test("fetchWithRetry retries on network errors thrown by fetch", async () => {
  const { calls, restore } = stubFetch([
    new TypeError("connection refused"),
    new Response('{"ok":1}', { status: 200 }),
  ]);
  try {
    const r = await fetchWithRetry(
      "https://example.test",
      { method: "POST" },
      [10, 10, 10],
      noSleep,
    );
    assert(r.status === 200, `expected eventual 200, got ${r.status}`);
    assert(calls.length === 2, `expected 2 fetches, got ${calls.length}`);
  } finally {
    restore();
  }
});
