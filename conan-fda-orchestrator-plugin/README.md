# conan-fda-orchestrator-plugin

Phase 0 skeleton for Conan v3.

## Layout

```
.claude-plugin/plugin.json   manifest
skills/                      4 sub-agent skills + ic_memo_polish (Phase 5)
mcp_servers/                 8 MCP servers — FastMCP Python (Phase 4.7)
hooks/hooks.json             observability + budget hooks
.mcp.json                    MCP server registration (orchestrator loads at runtime)
```

## Contents (planned)

### Skills (Phase 5)
- `sub_agent_literature_reviewer.md` — PubMed + preprints with citation graph expansion
- `sub_agent_competitive_landscape.md` — competitor pipeline analysis
- `sub_agent_regulatory_history.md` — FDA precedents + AdComm patterns
- `sub_agent_options_microstructure.md` — IV term structure + gamma + unusual activity
- `ic_memo_polish.md` — Cowork operator-triggered IC memo expansion (off critical path)

### MCP Servers (Phase 4.7)
- `pubmed_mcp.py` — PubMed E-utilities + Semantic Scholar citation graph
- `biorxiv_mcp.py` — bioRxiv + medRxiv preprint API
- `clinicaltrials_mcp.py` — ClinicalTrials.gov v2
- `openfda_mcp.py` — approvals, warning letters, FAERS, Orange Book, drug labels
- `fda_adcomm_mcp.py` — calendar, transcripts, briefing PDFs, panel composition
- `polygon_mcp.py` — quotes, options chain, IV term structure, gamma, unusual options, news
- `internal_rag_mcp.py` — hybrid_search across literature/internal/adcomm/post_mortems corpora, rerank
- `compute_mcp.py` — base_rate, brier_calibration, similar_resolved_cases, isotonic, verify_claim

### RAG Stack (Phase 4.5, lives in `modal_workers/rag/` not in plugin)
- voyage-3-large embeddings (Matryoshka 1024 for literature, 2048 elsewhere)
- voyage rerank-2.5
- Anthropic Contextual Retrieval (Haiku-augmented chunks)
- Postgres FTS hybrid + RRF k=60
- Section-aware late chunking
- Semantic Scholar citation graph (literature only)

## Distribution

Plan ref: open decision #9. This skeleton lives inside the main `marazuela/conan`
repo for Phase 0; will be extracted to its own repo `marazuela/conan-fda-orchestrator-plugin`
once Pedro confirms.

See `/Users/Pico/.claude/plans/confirm-orchestrator-cuddly-bubble.md` for the full
architectural spec.
