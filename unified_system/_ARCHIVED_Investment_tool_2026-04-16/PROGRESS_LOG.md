
## 2026-04-13 — AXSM Thesis v4 (final)
✅ Re-validated all facts via live web (not memory): clinical, regulatory, epidemiology, competitive, financial
✅ Corrected v3 errors: removed unverifiable ADVANCE-2 P=.004, separated ACCORD-1/ACCORD-2 data (different P-values), softened "top-decile" claim, added Symbravo FY2025 revenue
✅ Removed flagged sentence ("position sizing — not direction — is the real question") — verified absent in PDF (0 matches)
✅ Added upfront Glossary table with 22 acronym definitions; acronyms expanded on first use throughout
✅ Self-review pass: accuracy, logic, completeness, adversarial, calibration, source, creativity, data freshness, signal validity, narrative all checked
✅ Generated v4 DOCX (27KB, 342 paragraphs, schema validated), converted to PDF (229KB)
✅ Archived v3 to reporting_layer/archive/2026-04-13_axsm_v1/ as _v3.docx/_v3.pdf
⏭️ Next: monitor PDUFA approach; consider options structure sizing the day before action date

---

## Migration — 2026-04-15 — Reporting Hub consolidation

✅ Completed:
- `Investment tool/reporting_layer/` retired. Contents migrated to project-root `Reporting Hub/`:
  - `investment_theses/` → `Reporting Hub/investment_theses/` (schema_version 2.0, `source_tool: investment_tool` added to each entry; docx/pdf paths repathed from `reporting_layer/investment_theses/...` to `investment_theses/...`)
  - `performance_reports/` → `Reporting Hub/performance_reports/investment_tool/`
  - `archive/`, `working/`, `candidate_deep_dives/`, `deep_dives/`, `README.md` → `Reporting Hub/archive/2026-04-15_pre_hub_migration/investment_tool_*/`
- Scheduled tasks `investment-tool-performance-report` and `investment-tool-deep-dives` retired; replaced by consolidated `reporting-hub-performance` (daily 02:30 UTC) and `reporting-hub-deep-dives` (every 4h at :30 UTC) that read from `investment_discovery_system/` and write only to `Reporting Hub/`.
- `investment_discovery_system/` is unchanged. This system is now producer-only: it does not write outside its own folder.

⚠️ For future sessions:
- Historical references to `reporting_layer/` in earlier log entries reflect pre-migration state — do not attempt to reconstitute that folder.
- Hub read contract is authoritative in `Reporting Hub/SOURCES.md`.
