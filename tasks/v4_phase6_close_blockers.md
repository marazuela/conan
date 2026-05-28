# v4 Phase 6 close-out blockers — for Pedro

**Filed:** 2026-05-28 (during Phase 6 closure session)
**Status of overall Phase 6:** 6a/6b/6c code landed; Cowork-side 6b teardown done in this session; **the orchestrator pipeline has been silent since 2026-05-25 12:13 UTC** because of the two blockers below — neither of which is Phase 6 work itself.

---

## B2 — Anthropic API credit balance exhausted

Every Tier-1 `orchestrator_run` since `2026-05-27 16:00:00 UTC` failed with:

```
Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error',
  'message': 'Your credit balance is too low to access the Anthropic API.
              Please go to Plans & Billing to upgrade or purchase…'}}
```

Examples (all `tier=1, status=failed, trigger_type=catalyst_proximity`):

| run_id | asset_id | created_at |
|---|---|---|
| `bd5ad8b0-e6db-4144-bd3b-980979055a88` | `1847f617-…` | 2026-05-27 16:00:00 |
| `bcf98cf1-3dee-41d6-b832-f66971801c31` | `525e0a4e-…` | 2026-05-27 16:00:00 |
| `fa246280-ee55-4e73-b934-e3f791e6a4cd` | `dc510e90-…` | 2026-05-27 16:00:00 |

**Action:** top up Anthropic credits on the Modal-side org key (`anthropic-orchestrator` secret). Until you do, no Tier-1 run can land — Phase 6c is technically merged but no `orchestrator_version='orch-v4.0'` row has ever existed on live.

**Gating:** Day-1-after-6c verification (`run_one` against a real asset + `orchestrator_version='orch-v4.0'` row in `convergence_assessments`) is blocked on this.

---

## B4 — Supabase 15 s read timeouts at 2026-05-27 12:00 UTC

Two Tier-1 runs at the 12:00 catalyst_proximity sweep failed with:

```
HTTPSConnectionPool(host='xvwvwbnxdsjpnealarkh.supabase.co', port=443):
  Read timed out. (read timeout=15.0)
```

| run_id | asset_id | created_at | latency_to_fail |
|---|---|---|---|
| `f8060e91-1da3-4c74-a37e-ae7e6352ceac` | `bddbcf05-…` | 12:00:00 | 9 m 38 s |
| `443282d2-5bca-481b-98f2-b4caa81c21e7` | `f9bd5f6f-…` | 12:00:00 | 9 m 18 s |

Hypothesis (not investigated this session): pg_net saturation during the
catalyst_proximity sweep when the orchestrator does many parallel
`information_schema` / `convergence_assessments` lookups against a Supabase
edge runtime that's also handling reactor + fanout traffic. The 15 s timeout
is the requests-side default in `orchestrator_runtime/client.py`-style
boundaries.

**Action:** investigate separately. Not part of Phase 6 close-out. Possible
levers: bump the per-request timeout, throttle the catalyst_proximity sweep,
move heavy lookups to a service-role RPC instead of REST + jsonb.

---

## Done this session (not blockers — recorded here for the audit trail)

- **B3 — Phase 6b Cowork teardown completed.** `conan-cowork-skills` commit
  `181b9c6` deletes `skills/bulk_orchestrator_run.md` + `wrappers/bulk_orchestrator_run.md`
  and strips watchdog references. Local scheduled-task dirs
  `~/.claude/scheduled-tasks/bulk_orchestrator_priority1` + `priority2`
  removed. Critical operator_flag `2cefc672-c8e3-4306-9680-77333e1329c4`
  (`tier2_modal_actions_missing`) resolved.

- **B1 — Persist-assessment NULL-id regression fix already on live DB.**
  `persist_assessment_v3` body has `COALESCE(..., gen_random_uuid())`.
  Local migration file `20260527000000_persist_assessment_v3_null_id_fix.sql`
  was deleted by PR #155 (R-3 migration audit), but live is correct.
  No repo action needed.

- **Forward-port** of `4a28765` + `032c43f` from `feat/v4-foundation` to main
  → no-op. Both already on main via PR #92 salvage.
  `feat/v4-foundation` is now fully dead history relative to `main`.
