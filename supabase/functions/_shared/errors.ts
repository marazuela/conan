// Format an unknown thrown value (Error, PostgrestError-shaped, Resend error
// body, etc.) into a structured payload safe to serialize into a 500 response
// or stamp into a DLQ row.
//
// Why: the JS catch idiom `err instanceof Error ? err.message : String(err)`
// silently coerces Supabase/PostgREST errors — which are plain `{message,
// details, hint, code}` objects, not Error instances — to the literal string
// "[object Object]", losing all forensic detail.

export interface FormattedError {
  message: string;
  details?: unknown;
  code?: unknown;
  hint?: unknown;
}

export function formatError(err: unknown): FormattedError {
  if (err instanceof Error) {
    return { message: err.message };
  }
  if (err && typeof err === "object") {
    const o = err as Record<string, unknown>;
    const message = typeof o.message === "string"
      ? o.message
      : (() => {
        try {
          return JSON.stringify(o);
        } catch {
          return String(err);
        }
      })();
    const out: FormattedError = { message };
    if (o.details !== undefined) out.details = o.details;
    if (o.code !== undefined) out.code = o.code;
    if (o.hint !== undefined) out.hint = o.hint;
    return out;
  }
  return { message: String(err) };
}

// Flatten a FormattedError into a single string suitable for plain-text
// columns like `failed_reactor_events.error_message`.
export function formatErrorForDlq(err: unknown): string {
  const info = formatError(err);
  const parts: string[] = [info.message];
  if (info.code !== undefined) parts.push(`code=${String(info.code)}`);
  if (info.hint !== undefined) parts.push(`hint=${String(info.hint)}`);
  if (info.details !== undefined) {
    const d = typeof info.details === "string"
      ? info.details
      : (() => {
        try {
          return JSON.stringify(info.details);
        } catch {
          return String(info.details);
        }
      })();
    parts.push(`details=${d}`);
  }
  return parts.join(" | ");
}
