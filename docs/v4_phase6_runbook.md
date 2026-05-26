# v4 Phase 6 runbook

Step-by-step verification + rollback procedures for the three-stage Phase 6
landing of the v4 architecture simplification. Use this in order:

  Phase 6a → 7-day observation → Phase 6b → 14-day observation → Phase 6c

Companion to `~/.claude/plans/proud-booping-seal.md` (the plan). The plan
captures *why*; this runbook captures *what to type, in what order, and what to
check*.

---

## Phase 6a — Flag flip to v4 default

The commit (`2626554` on `feat/v4-foundation`) changes the env-read semantics:
`os.environ.get("ORCH_V4", "1") != "0"`. v4 runs by default; `ORCH_V4=0` is
the rollback.

### Pre-flip readiness gate (must all be green before merge)

| Check | How to verify | Command |
|---|---|---|
| Sibling-repo schema landed | `commercial_opportunity_v1.json` present on Cowork machine | `ls $CONAN_ROOT/../conan-cowork-skills/schemas/commercial_opportunity_v1.json` |
| Sibling-repo skill landed | `thesis_transcriber.md` present on Cowork machine | `ls $CONAN_ROOT/../conan-cowork-skills/skills/thesis_transcriber.md` |
| v2 rubric seed applied | Active rubric row for binary_catalyst has `insider_pressure` + `shareholder_structure` | `SELECT dimension_weights FROM rubrics WHERE profile='binary_catalyst' AND superseded_at IS NULL` |
| Shadow validation passed | ≥5 v4 dry-runs side-by-side with v3 baseline | See "Shadow validation" below |
| Baseline metrics captured | 7d-trailing cost/run + throughput + failure rate frozen as the comparison numbers | See "Baseline capture" below |

### Shadow validation (≥5 assets, dry-run, no DB writes)

For each of 5 known-good assets, run twice — once with ORCH_V4=0 (v3
baseline), once with ORCH_V4=1 (v4). Compare outputs.

```bash
# Pick 5 assets with recent activity and a known regulatory catalyst:
ASSETS=( "VRDN-asset-uuid" "AXSM-asset-uuid" "IONS-asset-uuid" "<two more>" )

for AID in "${ASSETS[@]}"; do
  echo "=== $AID v3 baseline ==="
  ORCH_V4=0 modal run modal_workers/orchestrator_app.py::orchestrator_run_one \
    --asset-id $AID --trigger-type manual --dry-run true 2>&1 | tee /tmp/v3_$AID.log

  echo "=== $AID v4 ==="
  ORCH_V4=1 modal run modal_workers/orchestrator_app.py::orchestrator_run_one \
    --asset-id $AID --trigger-type manual --dry-run true 2>&1 | tee /tmp/v4_$AID.log
done
```

For each pair, check by hand:
- **Direction**: same `thesis_direction` in 4/5 cases (a 1/5 flip is tolerable
  if commercial dims explain it; a 2/5 flip means investigate before merging).
- **Conviction**: v4 within ±15 pts of v3 on the same evidence.
- **commercial_dimensions populated**: every v4 dry-run prints a non-empty
  `commercial_dimensions` block.
- **Cost**: v4 single-shot should print measurably lower `cost_usd` than
  v3 multi-stage. Target ~30-40% reduction; <20% suggests v4 isn't actually
  collapsing the stages it should.

### Baseline capture (snapshot before merge)

Run these queries on the live Supabase project and save the output. These are
the comparison data for the 7-day observation gate.

```sql
-- 7-day mean cost per assessment (Tier-1 only)
SELECT round(avg(cost_usd)::numeric, 4) AS mean_cost_usd_7d
FROM convergence_assessments
WHERE tier = 1
  AND created_at > now() - interval '7 days';

-- 7-day daily assessment count
SELECT date_trunc('day', created_at)::date AS day,
       count(*) AS assessments
FROM convergence_assessments
WHERE created_at > now() - interval '7 days'
GROUP BY 1 ORDER BY 1;

-- 7-day failed_reactor_events / operator_flags rate
SELECT 'failed_reactor_events' AS source, count(*) AS rows_7d
FROM failed_reactor_events
WHERE created_at > now() - interval '7 days'
UNION ALL
SELECT 'operator_flags', count(*)
FROM operator_flags
WHERE created_at > now() - interval '7 days'
  AND severity IN ('warn','critical');
```

Paste the output into a `~/.claude/plans/phase6a_baseline_<YYYY-MM-DD>.txt`
file. The 7-day post-flip metrics get compared against this.

### Flip-day procedure

```bash
# 1. On the Conan repo: confirm Phase 6a is the merge target.
git log feat/v4-foundation --oneline | head -10

# 2. Merge to main. The Phase 6a commit is the SOLE change in the merge —
#    don't bundle other work that increases the blast radius.
gh pr create --base main --head feat/v4-foundation \
  --title "v4 Phase 6a: flag flip to v4 default" \
  --body "Phase 6a per ~/.claude/plans/proud-booping-seal.md"

# 3. After merge: deploy the orchestrator Modal app.
modal deploy modal_workers/orchestrator_app.py

# 4. Smoke: trigger one manual run on a known-quiet asset.
modal run modal_workers/orchestrator_app.py::orchestrator_run_one \
  --asset-id <some-watchlist-asset> --trigger-type manual
```

### Day-1-after-flip checks

```sql
-- v4 should be the only orchestrator_version_v4=true producer.
SELECT orchestrator_version_v4, count(*)
FROM convergence_assessments
WHERE created_at > now() - interval '24 hours'
GROUP BY 1;

-- Sanity on commercial_dimensions being populated for v4 rows.
SELECT count(*) FILTER (WHERE commercial_dimensions IS NOT NULL) AS with_commercial,
       count(*) FILTER (WHERE commercial_dimensions IS NULL) AS without_commercial
FROM convergence_assessments
WHERE orchestrator_version_v4 = true
  AND created_at > now() - interval '24 hours';

-- No new v4-specific failure mode in DLQ.
SELECT count(*) FROM failed_reactor_events
WHERE created_at > now() - interval '24 hours'
  AND (payload->>'source' LIKE 'sub_agent.commercial_opportunity'
       OR error_message ILIKE '%STAGE_1_V4%'
       OR error_message ILIKE '%commercial_dimensions%');
```

### Rollback (anytime in the 7-day window if quality regresses)

```bash
# 1. Set ORCH_V4=0 as a Modal secret default. No git revert needed.
modal secret create orch-flags ORCH_V4=0 --force

# 2. Add the secret to orchestrator_run_one + orchestrator_drain_queue if
#    not already on the secrets list (one-line edit).

# 3. Redeploy.
modal deploy modal_workers/orchestrator_app.py

# 4. Verify the next assessment lands with orchestrator_version_v4=false.
SELECT id, orchestrator_version_v4, created_at
FROM convergence_assessments
ORDER BY created_at DESC LIMIT 5;
```

The v3 codepath is still in the code (Phase 6c hasn't deleted it yet), so
this is a clean rollback — no data loss, no in-flight runs killed.

### 7-day observation gate (decision: proceed to 6b or rollback?)

Run the same baseline queries against the post-flip 7-day window. Compare:

| Metric | Target | Hard fail |
|---|---|---|
| `mean_cost_usd_7d` | 30-40% lower than baseline | Higher than baseline → investigate |
| daily assessment count | within ±10% of baseline | >20% drop → investigate |
| `failed_reactor_events` 7d | within ±20% of baseline | >50% increase → rollback |
| `operator_flags` warn+critical | within ±20% of baseline | >50% increase → rollback |

Manual quality spot check: sample 10 v4 assessments randomly, read the
`reasoning_trace` + `commercial_dimensions`, judge:
- Are the SoC drugs real and current for the indication?
- Is `unmet_need_severity_1_5` defensible given the disease?
- Does the commercial section meaningfully change the conviction vs a v3-style
  pure-regulatory thesis? (If always "no effect", the dual-mandate isn't earning
  its cost.)

**If all targets met:** proceed to Phase 6b.
**If hard-fail any metric:** rollback, investigate, fix, restart 7-day window.

---

## Phase 6b — Tier-2 deletion (after 7-day Phase 6a observation passes)

Plan reference: `~/.claude/plans/proud-booping-seal.md` § Phase 6b.

Order of operations is **strict** — applying the migration before deleting code
prevents orphaned Modal calls; deleting code before the Cowork-side teardown
leaves the bulk_orchestrator skill calling deleted RPCs (logs 404, doesn't
crash, but noisy).

### Order

1. **Pre-flight: drain in-flight Tier-2 rows**
   ```sql
   -- Check before:
   SELECT count(*) FROM orchestrator_runs WHERE tier=2 AND status='pending';
   SELECT count(*) FROM orchestrator_runs WHERE tier=2 AND status='running';
   -- If running > 0, wait for them to finish (they have a soft timeout;
   -- max ~1 hour).
   ```
2. **Apply migration `20260614000010_v4_drop_tier2.sql`** (write per plan).
3. **Cowork-side teardown** on JGoror's Windows machine:
   - Delete the scheduled task firing `bulk_orchestrator_run`.
   - Remove `bulk_orchestrator_run.md` + `bulk_orchestrator.md` from the
     `conan-cowork-skills` repo (commit + push from JGoror's machine).
4. **Modal code deletion** (this repo, commit on `feat/v4-foundation`):
   - Delete `orchestrator_runtime/tier2.py`
   - Remove three `@app.function` blocks from `modal_workers/orchestrator_app.py`
   - Remove entries from `COMPUTE_V3_ACTIONS` and `_dispatch_compute_v3_action`
   - Update `runtime.py:84` if it imports anything from `tier2`
   - Delete `nightly_calibration_refit.py` Tier-2 quality gate (constants +
     functions + dataclass per plan)
   - Delete `orchestrator_runtime/tests/test_tier2.py`,
     `modal_workers/tests/test_tier_quality_gate.py`, and Tier-2 references in
     `test_compute_v3_dispatch.py`
5. **Reactor edge function** (`supabase/functions/reactor/index.ts`): change
   the `cross_source` vs `new_doc` branch to always enqueue `tier=1`.
6. **Deploy + verify** — see "Day-1 checks" below.

### Day-1-after-6b checks

```sql
-- No new tier=2 rows after migration.
SELECT count(*), max(created_at)
FROM convergence_assessments
WHERE tier = 2 AND created_at > now() - interval '24 hours';
-- Expect 0.

-- No new rpc_tier2_* call failures in pg_net log.
SELECT count(*) FROM net._http_response
WHERE created > now() - interval '24 hours'
  AND error_msg ILIKE '%rpc_tier2%';
-- Expect 0.

-- Historical tier=2 rows still queryable (must NOT be 0).
SELECT count(*) FROM convergence_assessments WHERE tier = 2;
-- Should be a healthy number matching pre-deletion count.
```

### Rollback (within 14-day window)

`git revert <6b-commit>` + re-apply migration's `-- ROLLBACK:` block (write
this in the migration as a comment for safety). Cowork-side: re-add the
scheduled task on JGoror's machine.

### 14-day observation gate → proceed to 6c

Same comparison method as 6a. Add:

- No tier=2 rows created in the 14-day window.
- No Cowork-side scheduled-task complaints from JGoror.
- `convergence_assessments` daily count stable (v4 absorbs the volume that
  Tier-2 previously handled via `watch_priority` cadence).

---

## Phase 6c — v3 codepath removal (after 14-day Phase 6b observation passes)

Plan reference: `~/.claude/plans/proud-booping-seal.md` § Phase 6c.

This is the largest deletion. Once landed, rolling back requires git revert —
the v3 code is gone, so the `ORCH_V4=0` rollback path no longer works.

### Pre-flight readiness

- Phase 6b has been live for ≥14 days with no rollbacks.
- All v4 production rows since 6b carry `orchestrator_version_v4 = true`. SQL:
  ```sql
  SELECT orchestrator_version_v4, count(*)
  FROM convergence_assessments
  WHERE created_at > (SELECT max(created_at) FROM convergence_assessments
                      WHERE tier = 2)
  GROUP BY 1;
  -- All rows post-6b should be orchestrator_version_v4=true.
  ```
- `ORCH_V4=0` env var setting hasn't fired in the last 14 days (search logs).

### Order

1. **Lift the deterministic citation resolver from `constitutional.py`** into
   `runtime.py` first, BEFORE deleting `constitutional.py`. Rename to
   `_validate_citations(...)` and call it directly from the Stage 7 spot.
2. **Code surgery in `runtime.py`** per plan:
   - Delete `if is_v4:` branch (lines ~2265-2280); knobs become unconditional.
   - Delete `if ensemble_n > 1:` block (~109 lines).
   - Delete `if enable_premortem:` block (~129 lines).
   - Delete `ALL_FALSIFIED_CONVICTION_CEILING` constant + references.
   - Delete `STAGE_1_SYSTEM`, `STAGE_9_SYSTEM` (v3 constants); rename
     `STAGE_1_V4_SYSTEM` → `STAGE_1_SYSTEM`, `STAGE_9_V4_SYSTEM` → `STAGE_9_SYSTEM`.
   - Delete imports from `hypothesis`, `premortem`, `constitutional`.
     Keep `from orchestrator_runtime.ensemble import ...` only if Tier-3
     backtest opt-in still references it; otherwise delete.
   - Simplify `stage_10_persist` signature (drop `hypothesis_result`,
     `premortem_result` parameters; columns persist as NULL on new rows).
   - Drop `ensemble_n`, `ensemble_mode`, `enable_premortem`,
     `constitutional_skip_semantic` from `_run_one_inner` signature (they're
     all forced now). Same for the CLI args in `main()` and
     `orchestrator_app.py::orchestrator_run_one`.
   - Bump `ORCHESTRATOR_VERSION` to `"orch-v4.0"`.
3. **Delete files** (verify imports first with `grep -r`):
   - `orchestrator_runtime/hypothesis.py`
   - `orchestrator_runtime/premortem.py`
   - `orchestrator_runtime/constitutional.py` (after lifting the resolver)
   - `orchestrator_runtime/ensemble.py` (only if Tier-3 didn't claim it)
4. **Delete or rewrite tests** per plan (the Phase 2/6a tests asserting
   `ORCH_V4` semantics no longer make sense once the flag is gone).
5. **Update docs**: `data_flow_diagram.md`, `PRD_unified_investment_research_v3.md`,
   `DECISIONS.md`, `docs/SKILLS_LAYOUT.md`.

### Day-1-after-6c checks

- Full test suite passes locally.
- `grep -r "from orchestrator_runtime.hypothesis\|from orchestrator_runtime.premortem" .`
  returns zero hits.
- One manual orchestrator_run_one against a real asset; output schema
  matches pre-deletion v4 runs.
- New rows carry `orchestrator_version = "orch-v4.0"`.

### Rollback path (degraded)

`git revert <6c-commit>` is the only path. No env-var rollback survives once
v3 code is deleted. Be confident before merging.

---

## When to abort the whole Phase 6 sequence

Roll back to Phase 5 + drop the Phase 6a commit if any of these fire in the
first 14 days:

- v4 quality drops material: ≥3 of 10 assessments produce factually wrong
  commercial_dimensions (made-up SoC drugs, miscategorized indication, etc.).
- v4 cost runs higher than v3 baseline (means stage-collapse isn't taking
  effect — investigate before re-attempting).
- The orchestrator hits a new class of `failed_reactor_events` that didn't
  exist on v3.
- Pedro's `tasks/` file shows ≥5 v4-related open operator_flags after 7 days.

Better to defer Phase 6b/6c by a quarter than to land a faulty production
default.
