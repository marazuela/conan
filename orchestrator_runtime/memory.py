"""MemoryStore — hierarchical loader/writer for the v3 memory hierarchy.

Backed by Supabase Storage, indexed by `memory_files` table:

  scope      | scope_id format                        | what it holds
  -----------|----------------------------------------|--------------------------------
  asset      | <asset_id>                             | distilled thesis summaries (Stage 10 output)
  indication | <indication_normalized>                | indication-level cohort priors
  reviewer_panel | <reviewer_panel_id>                | FDA panel composition + voting history
  reference_class| <reference_class_signature>        | base-rate refit notes
  sub_agent  | <role>/<asset_id> or <role>/<indication> | per-role accumulated state

Storage path schema: `memory/<scope>/<scope_id>.md`. Bucket = `memory` (already
provisioned by 20260506000010_v3_phase_0_1_schema.sql comments).

Stage 0 (`stage_0_load`) calls `MemoryStore.load_all(asset_id, indication, reviewer_panel_id)`
to fetch all four scopes in parallel; concatenated into the system prompt
cached layer (1h TTL — see Stream 3.5).

Stage 10 (`stage_10_persist`) calls `MemoryStore.write(scope, key, content)`
after persisting the convergence row. The asset-scope writeback is the
existing TODO (runtime.py around line 729 region — convergence row write).
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Dict, Optional

from modal_workers.shared.supabase_client import SupabaseClient

logger = logging.getLogger(__name__)

MEMORY_BUCKET = "memory"
VALID_SCOPES = {"asset", "indication", "reviewer_panel", "reference_class", "sub_agent"}


def _storage_path(scope: str, scope_id: str) -> str:
    """Storage path for a memory file. Mirrors the asset_id-based fan-out used
    elsewhere; sub_agent scope_id may already contain '/'."""
    safe_id = scope_id.replace("..", "_").replace(" ", "_")
    return f"{scope}/{safe_id}.md"


@dataclass
class MemoryBlobs:
    asset: Optional[str] = None
    indication: Optional[str] = None
    reviewer_panel: Optional[str] = None
    sub_agent: Optional[str] = None      # concatenated across roles for this asset/indication

    def as_text(self) -> str:
        """Concatenate scopes into a single system-prompt-ready blob, with
        section headers so the model can attribute claims to the right scope."""
        parts: list[str] = []
        if self.asset:
            parts.append(f"<memory scope=\"asset\">\n{self.asset}\n</memory>")
        if self.indication:
            parts.append(f"<memory scope=\"indication\">\n{self.indication}\n</memory>")
        if self.reviewer_panel:
            parts.append(f"<memory scope=\"reviewer_panel\">\n{self.reviewer_panel}\n</memory>")
        if self.sub_agent:
            parts.append(f"<memory scope=\"sub_agent\">\n{self.sub_agent}\n</memory>")
        return "\n\n".join(parts)

    def is_empty(self) -> bool:
        return not (self.asset or self.indication or self.reviewer_panel or self.sub_agent)


class MemoryStore:
    def __init__(self, client: Optional[SupabaseClient] = None):
        self.client = client or SupabaseClient()

    # ---------- read ----------

    def _read(self, scope: str, scope_id: Optional[str]) -> Optional[str]:
        if not scope_id:
            return None
        path = _storage_path(scope, scope_id)
        try:
            blob = self.client.read_cache(MEMORY_BUCKET, path)
        except Exception as exc:  # noqa: BLE001
            logger.debug("memory read miss scope=%s key=%s: %s", scope, scope_id, exc)
            return None
        if not blob:
            return None
        try:
            return blob.decode("utf-8", errors="replace")
        except Exception:
            return None

    def load_all(
        self,
        *,
        asset_id: Optional[str] = None,
        indication: Optional[str] = None,
        reviewer_panel_id: Optional[str] = None,
        sub_agent_key: Optional[str] = None,
    ) -> MemoryBlobs:
        """Parallel reads of all four scopes. Misses → None silently.

        sub_agent_key is normally `<role>/<asset_id>` or `<role>/<indication>`.
        Passing only one combined sub_agent_key returns at most one role's blob;
        Stage 0 typically calls this once per role it needs.
        """
        jobs: Dict[str, tuple[str, Optional[str]]] = {
            "asset": ("asset", asset_id),
            "indication": ("indication", indication),
            "reviewer_panel": ("reviewer_panel", reviewer_panel_id),
            "sub_agent": ("sub_agent", sub_agent_key),
        }
        results: Dict[str, Optional[str]] = {k: None for k in jobs}
        with ThreadPoolExecutor(max_workers=4) as ex:
            future_to_key = {
                ex.submit(self._read, scope, scope_id): out_key
                for out_key, (scope, scope_id) in jobs.items()
            }
            for fut in as_completed(future_to_key):
                key = future_to_key[fut]
                try:
                    results[key] = fut.result()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("memory load scope=%s err=%s", key, exc)
        return MemoryBlobs(
            asset=results["asset"],
            indication=results["indication"],
            reviewer_panel=results["reviewer_panel"],
            sub_agent=results["sub_agent"],
        )

    # ---------- write ----------

    def write(self, scope: str, scope_id: str, content: str) -> None:
        """Upsert a memory blob. Updates Storage + memory_files index."""
        if scope not in VALID_SCOPES:
            raise ValueError(f"invalid scope {scope!r}; must be one of {sorted(VALID_SCOPES)}")
        if not scope_id:
            raise ValueError("scope_id must be non-empty")
        path = _storage_path(scope, scope_id)
        payload = (content or "").encode("utf-8")

        try:
            self.client.write_cache(MEMORY_BUCKET, path, payload, content_type="text/markdown")
        except Exception as exc:  # noqa: BLE001
            logger.error("memory write failed scope=%s key=%s err=%s", scope, scope_id, exc)
            raise

        # Upsert into memory_files index (id is generated by Postgres; UNIQUE on scope+scope_id)
        try:
            self.client._rest(
                "POST", "memory_files",
                params={"on_conflict": "scope,scope_id"},
                json_body={
                    "scope": scope,
                    "scope_id": scope_id,
                    "storage_path": path,
                    "size_bytes": len(payload),
                },
                prefer="return=minimal,resolution=merge-duplicates",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("memory_files index update failed scope=%s key=%s: %s",
                           scope, scope_id, exc)
