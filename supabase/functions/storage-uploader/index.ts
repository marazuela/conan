// storage-uploader edge function — proxies content uploads to Supabase Storage
// using the service-role key. Auth-gated (mandatory) since F-153 (2026-05-14).
//
// Before F-153 the function was deployed with zero authentication: it accepted
// `{bucket, path, content}` from any caller and wrote via service-role to any
// bucket. The audit memo entry covers the blast radius (arbitrary storage
// writes; overwrite of legit alert HTMLs in `reports`; quota inflation).
//
// Acceptable callers (documented):
//   - Cowork-resident skills writing analysis output (coverage_auditor,
//     thesis_writer per modal_workers/app.py:401 comment) to the `reports`
//     and `candidates` buckets.
//
// Hardening since F-153:
//   - x-supabase-webhook-secret header required (matches reactor/fanout
//     pattern; project-scoped env var WEBHOOK_SECRET).
//   - Bucket allowlist enforced — only 'reports' and 'candidates' currently.
//     Other buckets (`filings`, `memory_files`, `scanner-caches`) are
//     written via their own dedicated paths and should NOT route through
//     this proxy. Extend allowlist only when a caller documents the need.
//   - Body size cap (5 MiB) mirroring modal_workers/app.py:storage_upload_endpoint.
//
// Why this function exists at all (vs. supabase-js client side upload):
//   Cowork-resident skills run in environments without the supabase-js
//   client and use plain HTTP. This function gives them an authenticated
//   storage entry point that doesn't require shipping the service-role
//   key to the skill runtime.

import "jsr:@supabase/functions-js/edge-runtime.d.ts";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const WEBHOOK_SECRET = Deno.env.get("WEBHOOK_SECRET") ?? "";

const ALLOWED_BUCKETS = new Set(["reports", "candidates"]);
const MAX_BODY_BYTES = 5 * 1024 * 1024; // 5 MiB

Deno.serve(async (req: Request) => {
  // Auth gate (mandatory). x-supabase-webhook-secret OR Bearer matching
  // edge-runtime SERVICE_KEY. Constant-time compare on both. Same shape
  // as reactor/index.ts to keep the auth model uniform across edge fns.
  const headerSecret = req.headers.get("x-supabase-webhook-secret") ?? "";
  const authz = req.headers.get("authorization") ?? "";
  const bearerToken = authz.toLowerCase().startsWith("bearer ") ? authz.slice(7) : "";
  const webhookOk = WEBHOOK_SECRET !== "" && timingSafeEqual(headerSecret, WEBHOOK_SECRET);
  const serviceOk = bearerToken !== "" && timingSafeEqual(bearerToken, SERVICE_KEY);
  if (!webhookOk && !serviceOk) {
    return new Response("unauthorized", { status: 401 });
  }

  try {
    const body = await req.json() as {
      bucket: string;
      path: string;
      content: string;
      contentType?: string;
    };
    const { bucket, path, content, contentType } = body;
    if (!bucket || !path || content === undefined) {
      return new Response(
        JSON.stringify({ ok: false, error: "missing bucket/path/content" }),
        { status: 400, headers: { "Content-Type": "application/json" } },
      );
    }
    if (!ALLOWED_BUCKETS.has(bucket)) {
      return new Response(
        JSON.stringify({
          ok: false,
          error: `bucket '${bucket}' not in allowlist [${[...ALLOWED_BUCKETS].join(",")}]`,
        }),
        { status: 403, headers: { "Content-Type": "application/json" } },
      );
    }
    const contentBytes = new TextEncoder().encode(content).length;
    if (contentBytes > MAX_BODY_BYTES) {
      return new Response(
        JSON.stringify({
          ok: false,
          error: `content exceeds ${MAX_BODY_BYTES}-byte cap (got ${contentBytes})`,
        }),
        { status: 413, headers: { "Content-Type": "application/json" } },
      );
    }

    const resp = await fetch(
      `${SUPABASE_URL}/storage/v1/object/${bucket}/${path}`,
      {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${SERVICE_KEY}`,
          "apikey": SERVICE_KEY,
          "Content-Type": contentType || "text/markdown",
          "x-upsert": "true",
        },
        body: content,
      },
    );
    const txt = await resp.text();
    return new Response(
      JSON.stringify({
        ok: resp.ok,
        status: resp.status,
        response: txt,
        path: `${bucket}/${path}`,
      }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    );
  } catch (e) {
    return new Response(
      JSON.stringify({ ok: false, error: String(e) }),
      { status: 500, headers: { "Content-Type": "application/json" } },
    );
  }
});

// Constant-time string compare — same impl as fanout/reactor.
function timingSafeEqual(a: string, b: string): boolean {
  const aBytes = new TextEncoder().encode(a);
  const bBytes = new TextEncoder().encode(b);
  const len = Math.max(aBytes.length, bBytes.length);
  let diff = aBytes.length ^ bBytes.length;
  for (let i = 0; i < len; i++) {
    const ax = i < aBytes.length ? aBytes[i] : 0;
    const bx = i < bBytes.length ? bBytes[i] : 0;
    diff |= ax ^ bx;
  }
  return diff === 0;
}
