"""Prompt-version registry for v4 Phase 9b.

The orchestrator's Stage 1 (synthesis) and Stage 9 (structured extraction)
system prompts live as module constants in orchestrator_runtime.runtime.
Phase 9b's job is to make every prompt-text revision traceable on disk
AND in the database so:

  1. Every convergence_assessment carries an FK to the exact prompt text
     it ran under (stage_1_prompt_version_id + stage_9_prompt_version_id,
     migration 20260528085919).

  2. The Phase 9c quarterly prompt retrospective can group resolved
     post-mortems by prompt version and identify which prompt revisions
     correlate with which accuracy regimes.

  3. The Phase 9e A/B harness can swap prompt text against the eval
     cassette without retroactively rewriting history — the cassette
     records the prompt version id, replay just looks it up.

Design notes
------------
- **Idempotent UPSERT.** `register_prompt(sb, stage, prompt_text)` hashes
  the text, UPSERTs against the (stage, prompt_hash) UNIQUE constraint
  (migration 20260528092000), and returns the row's UUID. Same text →
  same UUID forever; new text → new row, automatically supersedes the
  prior active row for that stage.

- **Best-effort, never blocks the orchestrator.** A registry call failure
  (Supabase down, schema not applied, etc.) returns None and the
  assessment persists with a NULL prompt_version_id. The Phase 9c retro
  filters NULL FKs out of its cohort; partial coverage is better than a
  hard halt on the hot path.

- **Per-process cache.** Each Modal container caches the registered UUID
  per (stage, prompt_hash) so we don't round-trip on every assessment.
  Cache is intentionally process-local — concurrent containers each
  register on first call; the UNIQUE constraint resolves the race.

- **Active-flag bookkeeping.** When a brand-new prompt text registers,
  the previous `is_active=true` row for the same stage is marked
  `superseded_at=now()` and `superseded_by=new_id`. Two simultaneous
  registrations of different texts will race on the "supersede prior
  active" step; the loser's update simply finds no `is_active=true` row
  to update and is a no-op. The end state is consistent: at most one
  active row per stage, all earlier rows superseded.

Plan: ~/.claude/plans/phases-6-and-7-staged-hedgehog.md (Phase 9b).
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from threading import RLock
from typing import Dict, Optional, Tuple

from modal_workers.shared.supabase_client import SupabaseClient


logger = logging.getLogger(__name__)


# Process-local cache. Key is (stage, prompt_hash); value is the UUID
# string returned by Supabase. Guarded by _CACHE_LOCK so concurrent
# Stage 10 persists in the same container don't all round-trip.
_CACHE: Dict[Tuple[str, str], str] = {}
_CACHE_LOCK = RLock()

# Stage labels — keep these stable; downstream queries use them.
STAGE_1 = "stage_1_system"
STAGE_9 = "stage_9_system"
VALID_STAGES = frozenset({STAGE_1, STAGE_9})


def hash_prompt(prompt_text: str) -> str:
    """SHA-256 of the prompt text. Hex digest is 64 chars — well within
    the prompt_hash TEXT column."""
    return hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()


def _short_version_label(prompt_hash: str) -> str:
    """Short human-readable label for the `version` column. The hash is
    the canonical identity; the label is for operator readability in
    DECISIONS.md / dashboards. Format: `v4-<first-8-hex>`."""
    return f"v4-{prompt_hash[:8]}"


def register_prompt(
    sb: SupabaseClient,
    stage: str,
    prompt_text: str,
    *,
    metadata: Optional[Dict[str, object]] = None,
) -> Optional[str]:
    """Return the prompt_versions.id UUID for `prompt_text` under `stage`.

    Idempotent:
      - cache hit: returns the cached UUID immediately.
      - DB hit: SELECT finds an existing (stage, hash) row, caches its
        id, returns it.
      - DB miss: UPSERT inserts a new row (race-safe via UNIQUE
        constraint), marks any prior `is_active=true` row for the stage
        as superseded, caches the new UUID, returns it.

    Returns None on registry failure (Supabase down, schema drift, etc).
    Caller must handle None gracefully — the v4 covenant is that prompt
    tracking is best-effort and never blocks the orchestrator hot path.
    """
    if stage not in VALID_STAGES:
        logger.warning("register_prompt: unknown stage %r; skipping", stage)
        return None

    prompt_hash = hash_prompt(prompt_text)
    cache_key = (stage, prompt_hash)

    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
        if cached:
            return cached

    try:
        existing = sb._rest(
            "GET",
            "prompt_versions",
            params={
                "select": "id",
                "stage": f"eq.{stage}",
                "prompt_hash": f"eq.{prompt_hash}",
                "limit": 1,
            },
        )
        if existing:
            uid = existing[0].get("id")
            if uid:
                with _CACHE_LOCK:
                    _CACHE[cache_key] = uid
                return uid

        # Not yet registered — insert + supersede prior active for the stage.
        meta = {"registered_at": datetime.now(timezone.utc).isoformat()}
        if metadata:
            meta.update(metadata)

        insert_payload = [{
            "stage": stage,
            "version": _short_version_label(prompt_hash),
            "prompt_hash": prompt_hash,
            "prompt_text": prompt_text,
            "metadata": meta,
            "is_active": True,
        }]
        inserted = sb._rest(
            "POST",
            "prompt_versions",
            json_body=insert_payload,
            prefer="return=representation,resolution=merge-duplicates",
        )
        if not inserted:
            logger.warning(
                "register_prompt: UPSERT returned no payload for stage=%s "
                "hash=%s — falling back to follow-up SELECT",
                stage, prompt_hash[:8],
            )
            # Defensive re-SELECT — UPSERT may return empty body if the
            # row already existed under a tighter ON CONFLICT path.
            follow_up = sb._rest(
                "GET",
                "prompt_versions",
                params={
                    "select": "id",
                    "stage": f"eq.{stage}",
                    "prompt_hash": f"eq.{prompt_hash}",
                    "limit": 1,
                },
            )
            if not follow_up:
                logger.error(
                    "register_prompt: follow-up SELECT also empty for "
                    "stage=%s hash=%s — registry write failed",
                    stage, prompt_hash[:8],
                )
                return None
            uid = follow_up[0].get("id")
        else:
            row = inserted[0] if isinstance(inserted, list) else inserted
            uid = row.get("id") if isinstance(row, dict) else None

        if not uid:
            logger.error(
                "register_prompt: no id returned for stage=%s hash=%s",
                stage, prompt_hash[:8],
            )
            return None

        # Best-effort: supersede the previous active row for the stage.
        # We do this AFTER the insert so the new row exists to point at.
        # A failure here doesn't unwind the insert — at worst we end up
        # with two is_active=true rows momentarily and the next cold
        # start cleans up.
        try:
            sb._rest(
                "PATCH",
                "prompt_versions",
                params={
                    "stage": f"eq.{stage}",
                    "is_active": "eq.true",
                    "id": f"neq.{uid}",
                },
                json_body={
                    "is_active": False,
                    "superseded_at": datetime.now(timezone.utc).isoformat(),
                    "superseded_by": uid,
                },
            )
        except Exception:  # noqa: BLE001 — non-fatal cleanup
            logger.exception(
                "register_prompt: supersede-prior-active failed for stage=%s "
                "(new row %s persists, two active rows transiently); next "
                "registrar will reconcile",
                stage, uid,
            )

        with _CACHE_LOCK:
            _CACHE[cache_key] = uid
        return uid

    except Exception:  # noqa: BLE001 — registry is best-effort
        logger.exception(
            "register_prompt: registry call failed for stage=%s; "
            "assessment will persist with NULL prompt_version_id",
            stage,
        )
        return None


def get_cached(stage: str, prompt_text: str) -> Optional[str]:
    """Read-only cache probe. Returns None if not cached (no DB round
    trip). Useful for tests + for situations where the caller wants to
    decide whether to register synchronously or defer."""
    prompt_hash = hash_prompt(prompt_text)
    with _CACHE_LOCK:
        return _CACHE.get((stage, prompt_hash))


def clear_cache() -> None:
    """Test-only: reset the process-local cache. Production code should
    never call this — module reload semantics handle cache lifetime."""
    with _CACHE_LOCK:
        _CACHE.clear()
