"""v3 ingestion adapters.

Each module here wraps an existing provider/scanner with the v3 ingestion
contract: fetch raw documents and write them through document_writer to the
`documents` table. No signal emission; orchestrator handles synthesis.

Modules:
  federal_register_ingest.py — Federal Register documents (FDA AdComm notices,
                               final rules, staff reviews)

Phase 1 progressively adds modules for openFDA, ClinicalTrials, EDGAR (8-K /
10-K / 10-Q / Form 4 / S-1 / 13D-G), DailyMed, FAERS, AdComm transcripts,
warning letters, 483s, PubMed, bioRxiv, Polygon news.
"""
