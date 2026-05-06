"""v3 extractor package.

Two Sonnet-driven workers that turn raw documents into the structured layer
the orchestrator reads:

  asset_linker.py        — classifies each `documents` row into `asset_documents`
                           with link_type + confidence + extracted_spans.
                           Sonnet (single-pass for one-asset MVP; two-pass when
                           multiple competing assets exist).
                           Phase 1 deliverable.

  sonnet_fact_extractor.py — extracts structured FDA-relevant facts from each
                           linked doc. One row per fact in `extracted_facts`
                           with verbatim evidence_quote + citation_span.
                           Phase 1 deliverable.

Both modules read the Anthropic API key from `ANTHROPIC_API_KEY` env var (set
by Modal secret in production; passed via subprocess env in local testing).
"""
