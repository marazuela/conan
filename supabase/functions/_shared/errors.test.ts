import { assertEquals } from "https://deno.land/std@0.224.0/assert/mod.ts";
import { formatError, formatErrorForDlq } from "./errors.ts";

Deno.test("formatError unwraps a real Error", () => {
  const info = formatError(new Error("boom"));
  assertEquals(info, { message: "boom" });
});

Deno.test("formatError unwraps a PostgrestError-shaped object", () => {
  const pgErr = {
    message: "Cannot coerce the result to a single JSON object",
    details: "Results contain 0 rows",
    hint: null,
    code: "PGRST116",
  };
  const info = formatError(pgErr);
  assertEquals(info.message, pgErr.message);
  assertEquals(info.code, "PGRST116");
  assertEquals(info.details, "Results contain 0 rows");
  assertEquals(info.hint, null);
});

Deno.test("formatError falls back to JSON for object without message", () => {
  const info = formatError({ status: 500, reason: "weird" });
  // No `message` key → JSON-stringify the whole object so info is preserved.
  assertEquals(info.message.includes("weird"), true);
});

Deno.test("formatError handles primitives", () => {
  assertEquals(formatError("plain string").message, "plain string");
  assertEquals(formatError(null).message, "null");
  assertEquals(formatError(undefined).message, "undefined");
  assertEquals(formatError(42).message, "42");
});

Deno.test("formatErrorForDlq flattens PostgrestError into pipe-joined string", () => {
  const pgErr = { message: "boom", details: "extra", hint: "try this", code: "P0001" };
  const s = formatErrorForDlq(pgErr);
  assertEquals(s.startsWith("boom"), true);
  assertEquals(s.includes("code=P0001"), true);
  assertEquals(s.includes("hint=try this"), true);
  assertEquals(s.includes("details=extra"), true);
});

Deno.test("formatErrorForDlq returns just message for plain Error", () => {
  assertEquals(formatErrorForDlq(new Error("oops")), "oops");
});
