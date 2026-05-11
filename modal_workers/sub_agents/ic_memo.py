"""ICMemoRunner — Phase 3A synthesis sub-agent.

Unlike the four specialists (literature/competitive/regulatory_history/
options_microstructure), the IC memo runner does NOT fetch fresh evidence —
it synthesizes the specialists' already-validated outputs plus the Stage 9
thesis into an investment-committee-ready memo. Output sections (per D-111
§2 layout): thesis, asymmetry, kill_conditions, position_sizing_logic,
summary, citations.

Persistence: 5th `fda_agent_reviews` row with `agent_kind='ic_memo'`. The
dashboard `<SubAgentPanels />` component is configured (per D-111 §2) to
render the IC memo panel always-expanded, with the four specialist panels
collapsed below.

Tool surface: none (synthesis-only). The runner overrides `build_user_content`
to splice the specialist payloads + thesis into a single user message.

Skill: `conan-fda-orchestrator-plugin/skills/ic_memo_polish.md` — already
authored (per memory: ic_memo_polish.md).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from .runtime import ROLE_REGISTRY, SubAgentRunner, ToolHandler

SKILL_PATH = (
    Path(__file__).resolve().parents[2]
    / "conan-fda-orchestrator-plugin" / "skills" / "ic_memo_polish.md"
)


def _format_specialist_block(role: str, payload: Dict[str, Any]) -> str:
    """Render one specialist's structured_output as a labeled JSON block."""
    if not payload:
        return f"### [{role}]\n(no review available — specialist did not produce output)\n"
    return (
        f"### [{role}]\n"
        f"```json\n{json.dumps(payload, indent=2, default=str)}\n```\n"
    )


class ICMemoRunner(SubAgentRunner):
    role = "ic_memo"
    skill_path = SKILL_PATH
    schema_filename = "ic_memo_v1.json"
    # Synthesis-only: no tool surface.
    tool_defs: List[Dict[str, Any]] = []
    # IC memo runs once per assessment after all specialists; small budget.
    max_turns = 2
    max_output_tokens = 6000

    def build_handler(self) -> ToolHandler:
        # No tools — but the base runtime calls build_handler() unconditionally,
        # so return a closure that raises for any tool call (defensive; the
        # model shouldn't be able to call tools because tool_defs is empty).
        def handle(name: str, inp: Dict[str, Any]) -> Dict[str, Any]:
            raise ValueError(
                f"ic_memo runner has no tools; received tool_use for {name!r}"
            )
        return handle

    def build_user_content(
        self, question: str, asset_context: Dict[str, Any],
    ) -> str:
        """Splice specialist payloads + thesis into the prompt body.

        `asset_context` is expected to contain:
          - asset: dict with ticker/drug_name/indication/etc.
          - specialists: dict[role -> payload] with the four specialist outputs
          - thesis: dict from Stage 9 with direction/conviction_pct/text
          - reference_class_anchor: optional dict from Stage 4
        """
        asset = asset_context.get("asset") or {}
        specialists = asset_context.get("specialists") or {}
        thesis = asset_context.get("thesis") or {}
        anchor = asset_context.get("reference_class_anchor")

        specialist_blocks = "\n\n".join(
            _format_specialist_block(r, specialists.get(r) or {})
            for r in (
                "literature", "competitive",
                "regulatory_history", "options_microstructure",
            )
        )

        thesis_block = (
            f"```json\n{json.dumps(thesis, indent=2, default=str)}\n```"
            if thesis else "(no Stage 9 thesis available)"
        )
        anchor_block = (
            f"\n\n## Reference-class anchor\n"
            f"```json\n{json.dumps(anchor, indent=2, default=str)}\n```"
            if anchor else ""
        )

        return (
            f"## Asset\n"
            f"```json\n{json.dumps(asset, indent=2, default=str)}\n```\n\n"
            f"## Stage 9 thesis\n{thesis_block}{anchor_block}\n\n"
            f"## Specialist outputs (each is the validated `structured_output` "
            f"from the corresponding sub-agent)\n\n"
            f"{specialist_blocks}\n\n"
            f"## Question\n{question or 'Synthesize an IC-ready memo.'}\n\n"
            f"Return ONLY a JSON object matching {self.schema_filename}. "
            f"No prose outside the JSON. Cite specialist sources via the "
            f"citations[] array (source ∈ literature/competitive/"
            f"regulatory_history/options_microstructure)."
        )


ROLE_REGISTRY["ic_memo"] = ICMemoRunner  # type: ignore[assignment]
