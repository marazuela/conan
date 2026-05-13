# Plan: B4 — Stage 8 calibration order (calibrate raw, then cap)

## Context

**Bug.** In [orchestrator_runtime/runtime.py:739, 762-766](orchestrator_runtime/runtime.py:739), `stage_10_persist` reads `conviction = parsed["conviction_pct"]` which has already been mutated to the **capped** value by the Stage 3 all_falsified ceiling at [runtime.py:1807](orchestrator_runtime/runtime.py:1807) (`parsed["conviction_pct"] = capped`, capped at 30). Then Stage 8 isotonic calibration runs on that capped value:

```python
calibrated = apply_isotonic_calibration(conviction / 100.0, active_curve["curve_data"]) * 100.0
```

This means the persisted `conviction_pct_calibrated` is `isotonic(min(raw, 30)/100)` instead of `min(isotonic(raw/100), 30)`. The calibration curve never sees the "should have been 80, capped to 30" data point on the way out.

**Why it matters.** Calibration's job is to correct miscalibrated raw conviction against realized outcomes. Capping FIRST hides the true model output from the calibration step. If calibration would, say, reduce a raw 80 to 65 (model is over-confident), the cap-then-calibrate order produces `isotonic(30/100) ≈ 30`, while calibrate-then-cap produces `min(65, 30) = 30`. In this case both happen to land at 30 — but for an `all_falsified` run with raw=70 where calibration would output 50 (still above cap), cap-then-calibrate produces `isotonic(30/100) ≈ 30` while calibrate-then-cap produces `min(50, 30) = 30`. So for hard caps the visible output is identical, BUT the **invariant `conviction_pct_calibrated = isotonic(raw_conviction_pct/100, curve)` for non-capped runs** is broken: after the bug, `conviction_pct_calibrated` may be `isotonic(capped)`, which can deviate from `isotonic(raw)` even on non-capped runs that pass through the Stage 8 read of the already-mutated `parsed`.

Wait — that last sentence needs checking. Re-reading: the Stage 3 cap only mutates `parsed["conviction_pct"]` when `overall_verdict == "all_falsified"` AND `capped < raw_conv`. So for non-all_falsified runs, the mutation never fires and Stage 8 sees the genuine value. **The bug is real but its blast radius is limited to all_falsified runs only.**

**Production data confirms zero current impact.** Live state in `convergence_assessments` today:

| metric | count |
|---|---:|
| total assessments | 37 |
| `pre_mortem_verdict='all_falsified'` rows | 0 |
| rows with active calibration curve | 0 |

The bug has never bitten. It will bite the moment either (a) the first all_falsified outcome fires OR (b) a calibration curve is activated (which is gated on D-103 paired-bootstrap on a future post_mortem cohort — not yet ready). **No backfill is needed.** This is a forward-looking correctness fix only.

**Why now.** Two reasons: (1) the audit surfaced it as a logical inversion, easy to fix while the math is fresh; (2) the curve is being prepared for activation soon — fixing the ordering before activation avoids producing a single distorted row.

## What changes

### Code change — one block in [runtime.py](orchestrator_runtime/runtime.py)

Move the all_falsified cap OUT of the inline Stage 3 block (which mutates `parsed["conviction_pct"]`) and INTO `stage_10_persist`, applied AFTER isotonic calibration. Keep the `pre_premortem_conviction` stash on `ctx` so the raw value still flows into `raw_conviction_pct`. The change is small but precise.

**Step 1 — runtime.py:1789-1807 (`_run_one_inner`, all_falsified branch):**

Currently:
```python
if premortem_result.overall_verdict == "all_falsified":
    try:
        raw_conv = float(parsed.get("conviction_pct") or 0.0)
    except (TypeError, ValueError):
        raw_conv = 0.0
    capped = min(raw_conv, ALL_FALSIFIED_CONVICTION_CEILING)
    if capped < raw_conv:
        logger.warning(
            "Stage 3 all_falsified: capping conviction_pct %.1f -> %.1f",
            raw_conv, capped,
        )
        ctx["pre_premortem_conviction"] = raw_conv
        ctx["conviction_capped_by_premortem"] = True
    parsed["conviction_pct"] = capped
```

Change to: **don't mutate `parsed["conviction_pct"]`**. Just stash the cap intent on `ctx`:
```python
if premortem_result.overall_verdict == "all_falsified":
    try:
        raw_conv = float(parsed.get("conviction_pct") or 0.0)
    except (TypeError, ValueError):
        raw_conv = 0.0
    if raw_conv > ALL_FALSIFIED_CONVICTION_CEILING:
        logger.warning(
            "Stage 3 all_falsified: conviction %.1f will be capped to %.1f "
            "AFTER Stage 8 calibration",
            raw_conv, ALL_FALSIFIED_CONVICTION_CEILING,
        )
        # ctx flags consumed by stage_10_persist to enforce the cap on
        # the calibrated value (not the raw). raw_conviction_pct still
        # records the pre-cap, pre-calibration model output per D-117.
        ctx["conviction_capped_by_premortem"] = True
    # Always set pre_premortem_conviction so the raw_conviction_pct
    # writeback path is consistent regardless of whether the cap binds.
    ctx["pre_premortem_conviction"] = raw_conv
    # Do NOT mutate parsed["conviction_pct"] — let calibration see the raw
    # value, then apply the cap to the calibrated output in stage_10_persist.
```

**Step 2 — runtime.py:739-773 (`stage_10_persist`):**

The existing code reads `conviction = parsed["conviction_pct"]` (now the raw value since step 1 stops mutating it), calibrates it, then derives band. Insert the cap AFTER calibration:

```python
# Stage 8 — isotonic calibration if a curve is active
active_curve = get_active_calibration_curve(sb)
if active_curve and active_curve.get("curve_data"):
    calibrated = apply_isotonic_calibration(
        conviction / 100.0, active_curve["curve_data"]) * 100.0
    calibrated = max(0.0, min(100.0, calibrated))
    calibration_curve_version: Optional[str] = active_curve.get("version")
else:
    calibrated = conviction
    calibration_curve_version = None

# B4: apply the Stage 3 all_falsified cap AFTER calibration. Capping before
# calibration (the old order) hid the raw model output from the curve and
# distorted conviction_pct_calibrated for all_falsified runs. The raw value
# still flows into raw_conviction_pct (from ctx["pre_premortem_conviction"])
# for the feedback loop.
if ctx.get("conviction_capped_by_premortem"):
    pre_cap_calibrated = calibrated
    calibrated = min(calibrated, ALL_FALSIFIED_CONVICTION_CEILING)
    if calibrated < pre_cap_calibrated:
        logger.info(
            "Stage 10: all_falsified cap applied AFTER calibration "
            "(%.1f -> %.1f)", pre_cap_calibrated, calibrated,
        )

band = derive_band(calibrated)
```

That's the whole code change. Estimated diff: ~25 lines.

### What the change preserves

- `raw_conviction_pct` writeback path: unchanged. Still records the pre-cap, pre-calibration model output, sourced from `ctx["pre_premortem_conviction"]`.
- The calibration curve training input (`raw_conviction_pct` in [nightly_calibration_refit.py:397](modal_workers/scripts/nightly_calibration_refit.py:397)): unchanged. The curve fits on the same raw values either way.
- D-103 paired-bootstrap promotion gate: unchanged. The fix doesn't refit, doesn't change the curve, doesn't change the gate math.
- The cap semantics: still "capped value can never exceed 30 when all_falsified." The cap continues to be a hard ceiling on the persisted `conviction_pct` and `conviction_pct_calibrated`.

### What changes for downstream

- For runs where Stage 3 returns `all_falsified` AND a calibration curve is active, the persisted `conviction_pct_calibrated` will now satisfy the invariant `conviction_pct_calibrated = min(isotonic(raw_conviction_pct/100, curve)*100, 30)` — previously it was `isotonic(min(raw, 30)/100)*100`, which silently lost the calibration on the raw signal.
- The dashboard's `band` derivation is downstream of `calibrated` — same value, just computed correctly.

## Backfill

**Skip.** Zero existing rows have `pre_mortem_verdict='all_falsified'` AND zero rows have a calibration curve active. The bug has produced no observable distortion in any historical row. Writing a backfill migration is pure ceremony.

If a calibration curve activates BEFORE this fix lands AND an `all_falsified` row gets persisted in between, then a one-row backfill UPDATE would be needed. Watch `convergence_assessments` for both conditions becoming non-zero and gate this plan's landing order accordingly:

```sql
SELECT
  COUNT(*) FILTER (WHERE pre_mortem_verdict = 'all_falsified') AS af_runs,
  COUNT(*) FILTER (WHERE calibration_curve_version IS NOT NULL) AS calibrated_runs
FROM convergence_assessments;
```

If either climbs above 0 before merge: pause, add a 5-line backfill migration. Today both are 0; ship without backfill.

## Files modified

| File | Change |
|---|---|
| `orchestrator_runtime/runtime.py` | ~25 lines: don't mutate `parsed["conviction_pct"]` on all_falsified; apply cap after Stage 8 calibration in `stage_10_persist`. |
| `orchestrator_runtime/tests/test_stage_8_calibration_order.py` (new) | ~150 lines: 4 tests covering the four conviction states (no cap × no curve, no cap × curve, cap × no curve, cap × curve). |

No migration. No backfill. No D-103 gate run.

## Tests

New file `orchestrator_runtime/tests/test_stage_8_calibration_order.py`:

1. **`test_no_cap_no_curve_persists_raw_unchanged`** — Mock everything; assert `conviction_pct_calibrated == raw_conviction_pct == parsed.conviction_pct`. Sanity baseline.

2. **`test_no_cap_with_curve_applies_calibration_to_raw`** — Active curve mock that doubles input (knots [{x:0,y:0},{x:0.5,y:1.0}]). Assert `conviction_pct_calibrated == 2 * raw_conviction_pct` (clamped). Confirms calibration sees the raw, not a mutated value.

3. **`test_all_falsified_no_curve_caps_at_30`** — Raw = 80, all_falsified, no curve. Assert `conviction_pct_calibrated == 30` AND `raw_conviction_pct == 80` AND `evidence_ledger.conviction_capped_by_premortem == true`. Cap still binds when curve absent.

4. **`test_all_falsified_with_curve_calibrates_then_caps`** — Raw = 70, all_falsified, identity curve. Assert `conviction_pct_calibrated == 30` (because raw=70 → isotonic(70) ≈ 70 → cap to 30). This is the bug-fix regression guard.

5. **`test_all_falsified_with_curve_that_lowers_raw_below_cap`** — Raw = 80, all_falsified, curve that halves input. Assert `conviction_pct_calibrated == 40` from isotonic, then capped to 30. Confirms cap is post-calibration.

Tests use the same `_baseline_ctx()` / `_stage4_anchor_stub()` patterns from `test_runtime_stage_7_gate.py` (already in this PR's adjacent file). They patch `get_active_calibration_curve` to return synthetic curves; no DB or LLM dependencies.

## Verification (post-implementation)

1. **Unit tests pass** — `pytest orchestrator_runtime/tests/test_stage_8_calibration_order.py -v`.

2. **No regression in adjacent stages** — `pytest orchestrator_runtime/tests/ -v` clean (run with `ORCHESTRATOR_MODEL=claude-sonnet-4-5-20250929` env override per the pre-existing `test_budget.py` issue).

3. **Live spot-check** — once a calibration curve is activated (post-fix), pick one asset and run the orchestrator twice with `--dry-run`: once with a synthetic `all_falsified` premortem (operator override) and once without. Assert:
   ```sql
   SELECT raw_conviction_pct, conviction_pct, conviction_pct_calibrated,
          (evidence_ledger->>'conviction_capped_by_premortem')::bool AS capped
   FROM convergence_assessments ORDER BY created_at DESC LIMIT 2;
   ```
   Expect: `raw_conviction_pct > conviction_pct_calibrated == 30` on the all_falsified run; `raw_conviction_pct == conviction_pct_calibrated` on the normal one (if curve is identity) or `raw_conviction_pct ≠ conviction_pct_calibrated` (if curve transforms).

4. **Production guard** — re-run the gate query above after merge to confirm the backfill skip is still valid:
   ```sql
   SELECT
     COUNT(*) FILTER (WHERE pre_mortem_verdict = 'all_falsified') AS af_runs,
     COUNT(*) FILTER (WHERE calibration_curve_version IS NOT NULL) AS calibrated_runs
   FROM convergence_assessments;
   ```
   Both should be 0 at merge time. If either is nonzero, add the backfill migration before merge.

## Rollout

This belongs in either:
- a follow-up commit on the existing `amazing-boyd-2f0641` worktree (alongside the B1+B2+B3 PR — package label "orchestrator: calibrate raw, cap after"), OR
- its own thin PR if the existing one is too large for review.

I'd recommend **same PR**, since the change is small (~25 lines), the test fixture overlaps with the new `test_runtime_stage_7_gate.py`, and the docstring updates reference D-117 which is also touched in the B1 fix. A follow-up commit keeps the diff coherent: "fix all_falsified cap to apply after Stage 8 calibration, not before."

## Followups deferred

- **D-117 amendment** — once this lands, update DECISIONS.md to note: "The cap on `conviction_pct` and `conviction_pct_calibrated` is applied AFTER Stage 8 isotonic calibration, not before. raw_conviction_pct records the pre-cap, pre-calibration model output."
- **Calibration curve activation gate** — if/when D-103 promotes the first curve, run the live spot-check above as part of the curve's smoke-test.
- **B5/B6/B7/B8/B9** — separate plans.
