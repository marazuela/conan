# Reporting Lock

**Status**: RELEASED

This lock is owned exclusively by the `unified-reporting` scheduled task. It is INDEPENDENT from `SESSION_LOCK.md` (which is held by operational + maintenance tasks). The reporting task is READ-ONLY to all operational data — it can safely run in parallel with operational/maintenance.

The reason we still have a lock: two concurrent reporting runs could race on writing to `reports/candidates_index.json` or produce duplicate PDFs. So reporting is serialized with itself, but not with operational work.

---

## Lock protocol (same as SESSION_LOCK)

1. Read this file.
2. If `RELEASED` — proceed.
3. If `HELD` and `held_until` > 4h in the past — force-release.
4. Otherwise abort with exit code 0.
5. Acquire = atomic write `HELD` with held_by/since/until.
6. On shutdown, atomic write back to `RELEASED`.

---

## Lock state

```
status: RELEASED
held_by: —
held_since: —
held_until: —
last_released_by: unified-reporting
last_released_at: 2026-04-20T09:04:41Z
```
