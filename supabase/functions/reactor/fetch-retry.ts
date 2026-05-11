// F-203: retry rubric_apply_caps on transient Modal failures before DLQ.
// Backoff schedule 200ms → 800ms → 3200ms (4× factor, ~4.2s worst case).
// Retry on network errors, 5xx, 408, 429. Do NOT retry on other 4xx —
// deterministic input bugs won't fix themselves. After exhausting retries,
// throw so the existing DLQ path (failed_reactor_events insert) still fires.

export const RUBRIC_RETRY_DELAYS_MS = [200, 800, 3200] as const;

export function isRetryableStatus(status: number): boolean {
  return status >= 500 || status === 408 || status === 429;
}

export async function fetchWithRetry(
  url: string,
  init: RequestInit,
  delays: readonly number[] = RUBRIC_RETRY_DELAYS_MS,
  sleep: (ms: number) => Promise<void> = (ms) =>
    new Promise((res) => setTimeout(res, ms)),
): Promise<Response> {
  let lastErr: unknown;
  for (let attempt = 0; attempt <= delays.length; attempt++) {
    try {
      const r = await fetch(url, init);
      if (r.ok) return r;
      if (isRetryableStatus(r.status)) {
        lastErr = new Error(
          `rubric_apply_caps ${r.status}: ${await r.text()}`,
        );
      } else {
        // Non-retryable HTTP error — return so caller can handle.
        return r;
      }
    } catch (err) {
      lastErr = err;
    }
    if (attempt < delays.length) await sleep(delays[attempt]);
  }
  throw lastErr;
}
