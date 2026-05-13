# IC Memo / Specialist Pipeline — Schema Drift & Plan

**Date filed:** 2026-05-13
**Status:** Read-side bridged (commit `8887773`); write-side still drifts
**Severity:** P2 (no live workflow currently depends on the write-side gap; will bite when IC memos start being synthesized)
**Owner:** unassigned
**Related memory:** [fda_agent_reviews_operator_fed](../memory/fda_agent_reviews_operator_fed.md), [v3_pipeline_scheduling_state](../memory/v3_pipeline_scheduling_state.md)
**Related commit:** [`8887773`](https://github.com/marazuela/conan/commit/8887773) — read-side bridge (2026-05-13)

---

## TL;DR

The orchestrator's Stage-2 specialist pipeline + Stage-10b IC memo synthesis straddled two tables: `sub_agent_calls` (what the orchestrator code writes) vs `fda_agent_reviews` (what `fda_signal_promote_to_thesis()` reads). Commit `8887773` shipped a **read-side bridge** so `load_ic_memo_context` falls back to `fda_agent_reviews` (Phase 0 3-role) when `sub_agent_calls` is empty. IC memo synthesis can now actually run — it consumes the 90 completed Phase 0 reviews.

The **write side is still drifted**: `persist_ic_memo_result()` inserts the synthesized IC memo into `sub_agent_calls` with `role='ic_memo'`. The dashboard's promote-to-thesis flow can't see it because the SQL RPC reads `fda_agent_reviews` with `agent_kind='ic_memo'`. So the moment someone clicks "Generate IC memo" then "Promote to thesis", the promote step will fail with `ic_memo review not found`.

This document captures what's true today + the remaining write-side fix needed (**Path A residual**).

---

## Findings (verified against live DB 2026-05-13)

### F-IC1 — Two parallel tables for the same conceptual thing

| | `sub_agent_calls` | `fda_agent_reviews` |
|--|-------------------|---------------------|
| Keyed by | `assessment_id` (→ convergence_assessments) | `event_id` (→ fda_regulatory_events) |
| Status field | none (presence = ran) | `queued / running / completed / failed` |
| Written by | orchestrator (`ic_memo_runner.persist_ic_memo_result`) | operator script (placeholders only — see F-IC3) |
| Read by | nothing live | `fda_signal_promote_to_thesis()` SQL RPC |
| Live rows | 0 | 138 (all placeholder, see F-IC3) |

These are **not renames of each other** — they're structurally different. `fda_agent_reviews` is event-scoped (one row per fda_regulatory_events × agent_kind); `sub_agent_calls` is assessment-scoped (one row per convergence_assessments × role).

The SQL RPC `fda_signal_promote_to_thesis(p_event_id, p_ic_memo_review_id, p_note)` verifies `v_review.event_id = p_event_id` — so the RPC's design intent is that IC memos are event-scoped, not assessment-scoped. The orchestrator code violates that.

### F-IC2 — Name mismatch on 2 of 4 specialist roles

`fda_agent_reviews.agent_kind` CHECK accepts:
```
medical, regulatory, microstructure, literature, competitive, ic_memo, aging_review
```

Orchestrator code (`sub_agent_dispatcher.py`, `ic_memo_runner._load_ic_memo_context`) uses:
```
literature, competitive, regulatory_history, options_microstructure
```

Two of four match (`literature`, `competitive`). Two don't:
- `regulatory_history` (code) ≠ `regulatory` (DB)
- `options_microstructure` (code) ≠ `microstructure` (DB)

If you ever wrote orchestrator-side specialist outputs to `fda_agent_reviews` today, two of four would `check_violation`.

### F-IC3 — Specialists run via an operator-fed pipeline, not the orchestrator

`fda_agent_reviews` state (2026-05-13):

| agent_kind | status=completed (non-empty) | status=queued (empty placeholders) |
|------------|------------------------------:|------------------------------------:|
| medical | 30 | 16 |
| regulatory | 30 | 16 |
| microstructure | 30 | 16 |

Completed rows are real structured_output from Phase 0 operator-fed runs (last ran 2026-05-13 ~07:44 UTC). The 48 queued rows are placeholders awaiting future runs.

`sub_agent_calls` (orchestrator-native table) has **0 rows total**. The orchestrator's Stage-2 dispatch is feature-flagged off:
```python
# orchestrator_runtime/runtime.py:102
ENABLE_SUB_AGENTS_DEFAULT = os.environ.get("ORCH_ENABLE_SUB_AGENTS") == "1"
```

**Read-side fix shipped** (`8887773`): `load_ic_memo_context` now falls back to `fda_agent_reviews` when `sub_agent_calls` is empty, mapping `medical→literature, regulatory→regulatory_history, microstructure→options_microstructure`. `competitive` has no Phase 0 equivalent and stays empty (renders a "no review available" placeholder in `build_user_content`).

So IC memo synthesis can actually run today against the 30 sets of Phase 0 reviews. It just hasn't been invoked yet because there's no cron firing it.

### F-IC4 — IC memo synthesis output still lands in the wrong table

`persist_ic_memo_result()` at `ic_memo_runner.py:195` still inserts into `sub_agent_calls`:

```python
rows = sb._rest("POST", "sub_agent_calls", json_body={
    "assessment_id": assessment_id,
    "role": IC_MEMO_ROLE,           # 'ic_memo'
    "query": question,
    "output": result.output,
    "schema_pass": result.schema_pass,
    ...
})
```

But `fda_signal_promote_to_thesis()` reads from `fda_agent_reviews`:

```sql
SELECT * INTO v_review FROM public.fda_agent_reviews WHERE id = p_ic_memo_review_id;
IF v_review.agent_kind <> 'ic_memo' THEN RAISE;
```

So after the orchestrator synthesizes an IC memo today:
- ✅ Row gets written (to `sub_agent_calls`)
- ❌ Dashboard "Promote to thesis" button can't find it (looks in `fda_agent_reviews`)
- ❌ Operator sees `ic_memo review {uuid} not found` error

Zero rows in `sub_agent_calls.role='ic_memo'` (nothing's been invoked). Zero rows in `fda_agent_reviews.agent_kind='ic_memo'` (orchestrator doesn't write there).

### F-IC5 — `convergence_assessments` has no `event_id` link

`convergence_assessments` columns related to triggering: `trigger_doc_id`, `trigger_type`. No `event_id`. So even if the orchestrator wanted to write an event-scoped IC memo, it doesn't carry the event id through the pipeline. The bridge would need a JOIN at IC-memo time:

```sql
-- "the next pending event for this asset"
SELECT id FROM fda_regulatory_events
WHERE asset_id = <assessment.asset_id>
  AND event_status = 'pending'
ORDER BY event_date NULLS LAST, created_at DESC
LIMIT 1;
```

Ambiguous when an asset has multiple pending events (multi-indication programs, parallel filings). The current design has no tie-breaker.

---

## Why this isn't broken today

Two things have to be true for the write-side drift to bite, and only one of them holds:

| Condition | Status |
|-----------|--------|
| IC memo synthesis being invoked (via `compute_v3 ic_memo_run` or a cron) | ❌ Not on any schedule; operator-trigger-only and unused at scale |
| Operator clicking "Promote to thesis" from the dashboard for an event with a synthesized IC memo | ⚠️ Path exists but the previous condition gates it |

So `convergence_assessments` keep getting written (37 total), the read-side bridge means future IC memo invocations would work, but the write-side gap waits to bite as soon as the synthesis path actually fires.

The read-side bridge is the load-bearing reason this isn't already a P1 — without it, every invocation would hard-fail with `ICMemoOrchestrationError`. With it, invocations succeed in synthesis but produce orphaned rows the dashboard can't see.

---

## Path A residual — what's left to fix

The read-side bridge (commit `8887773`) handled half of Path A. What remains:

### Scope of work (write-side only)

**1. Update `persist_ic_memo_result()` to write `fda_agent_reviews`.**

Replace the `sub_agent_calls` INSERT with an `fda_agent_reviews` INSERT keyed by event_id. Needs an `event_id` resolution helper because `convergence_assessments` doesn't carry event_id today:

```python
def _resolve_event_id_for_assessment(sb, asset_id) -> str | None:
    """The asset's most-recently-created pending event. None if asset has
    no pending events. Matches the same scoping used by _load_phase0_specialists()
    on the read side — keeps producer + consumer aligned on which event
    the IC memo belongs to."""
    events = sb._rest("GET", "fda_regulatory_events", params={
        "select": "id",
        "asset_id": f"eq.{asset_id}",
        "event_status": "eq.pending",
        "order": "created_at.desc",
        "limit": "1",
    }) or []
    return events[0]["id"] if events else None
```

Then in `persist_ic_memo_result`:

```python
event_id = _resolve_event_id_for_assessment(sb, assessment_asset_id)
if event_id is None:
    logger.warning("assessment %s has no pending events; skipping ic_memo persist",
                   assessment_id)
    return None  # honest-empty; caller handles

rows = sb._rest("POST", "fda_agent_reviews", json_body={
    "event_id": event_id,
    "agent_kind": "ic_memo",
    "version": ORCHESTRATOR_VERSION,        # e.g. "orch-v0.4.0-mvp"
    "structured_output": result.output,
    "confidence": result.output.get("confidence"),  # if schema provides
    "status": "completed",
    "ran_at": datetime.now(timezone.utc).isoformat(),
}, prefer="return=representation")
return rows[0]["id"] if rows else None
```

**Affected file:** only `orchestrator_runtime/ic_memo_runner.py` (~30 lines changed).

**2. No schema migration needed.** `fda_agent_reviews.agent_kind` CHECK already accepts `ic_memo` (verified live: `medical, regulatory, microstructure, literature, competitive, ic_memo, aging_review`). The 2/4 mismatch (`regulatory_history` vs `regulatory`, `options_microstructure` vs `microstructure`) only matters if you ever try to write SPECIALIST rows to `fda_agent_reviews` with the long names. The bridge in `_load_phase0_specialists` handles the read-side mapping; the orchestrator doesn't write specialist rows (operator-fed pipeline does).

**3. Tests.**
- `orchestrator_runtime/tests/test_ic_memo_orchestration.py` — update `persist_ic_memo_result` test to assert it INSERTs into `fda_agent_reviews` with the right shape.
- New integration: seed an `fda_regulatory_events` row + 3 completed Phase 0 reviews; run `run_ic_memo()`; assert `fda_agent_reviews` has a row with `agent_kind='ic_memo'` and `event_id=<seeded>` and the dashboard's SQL RPC can read it without error.

### Decisions still owed before execution

1. **Behavior when no pending event exists.** Today's plan: warn-log + skip persist (return None). Alternative: raise `ICMemoOrchestrationError` to surface the problem. — vote: **warn + skip** (lets non-eventful assessments still complete their assessment_stage_metrics + convergence_assessments writes; IC memo just doesn't get persisted).
2. **Backfill of existing `sub_agent_calls.role='ic_memo'` rows?** Zero rows exist, so n/a — but if a few accumulate between now and the fix, they'd orphan. Either re-run synthesis or migrate them. — vote: **no migration; rows orphan harmlessly since none have ever existed and few will between commit and fix.**

### Risks & gotchas (still valid)

- **`fda_regulatory_events` is upstream-broken** (memory `catalyst_universe_vs_fda_regulatory_events`). Only 35 rows, frozen at 2026-05-04. The bridge from `catalyst_universe` (fetcher-fed) to `fda_regulatory_events` (operator-script-fed) is missing. Path A unblocks IC memo synthesis-then-promote-flow but the bigger pipeline still depends on someone feeding events. This is a separate finding worth its own doc.
- **Cost when specialists fire (`ORCH_ENABLE_SUB_AGENTS=1`).** Each assessment triggers 4 specialist Sonnet+MCP loops — estimate $0.50–$2 added per assessment. At today's 5/day = $3–10/wk new spend. **This is the next-best Opus-scheduled-task candidate after this fix lands** — specialists are parallel-within-turn but independently dispatchable, perfect for subscription routing.

### Risks & gotchas

- **Cost blow-up.** Once Stage-2 dispatch fires for real, every assessment triggers 4 specialist Sonnet+MCP loops. Per memory it's "budgeted" but no live cost data exists (because it's never run). Estimate per assessment: $0.50–$2 added (4 specialists × ~200 tokens out × MCP turns). At today's ~5 assessments/day = $3–10/wk new spend. **This is also the next-best Opus-scheduled-task candidate after this fix lands** — specialists are parallel-within-turn but independently dispatchable, perfect for subscription routing.
- **Event_id resolution is brittle.** Assets with no pending events but an active assessment will skip IC memo entirely. Need to decide: skip silently, warn-log, or refuse to run the assessment until an event exists.
- **`status='queued'` placeholder rows.** The 138 existing rows are `agent_kind` in (medical, regulatory, microstructure) which DON'T match the new orchestrator-written kinds. So they coexist harmlessly. Decide if they should be archived/deleted as part of this fix or left as legacy.
- **`fda_regulatory_events` is upstream-broken** (memory `catalyst_universe_vs_fda_regulatory_events`). Only 35 rows, frozen. The bridge from `catalyst_universe` (fetcher-fed) to `fda_regulatory_events` (operator-script-fed) is missing. Path A unblocks IC memo synthesis but the bigger pipeline still depends on someone feeding events.

### Estimated effort

With the read-side bridge already shipped, the remaining write-side work is much smaller:
- 2h: code changes (~30 lines in `ic_memo_runner.py`)
- 2h: update tests, integration smoke
- 1h: deploy + verify against one test event end-to-end

**Total: ~half an engineer-day.**

Plus open-ended Opus-scheduled-task migration of the 4 specialists once write-side is fixed.

---

## When this becomes urgent

Watch for any of:
- `ORCH_ENABLE_SUB_AGENTS=1` being set in Modal secret (Pedro intent to turn specialists on for real)
- Anyone scheduling an `ic_memo_run` cron (today the path is operator-trigger-only)
- A second operator-fed batch of `fda_regulatory_events` (would re-light the promote-thesis workflow at scale)
- Anyone reporting "why is the promote-to-thesis button broken?"

Until then, this sits as a known issue with the write-side fix planned and scoped above.
