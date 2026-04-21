# Conan — Investment Research System

## Where to look

**Every time you want to see the current candidate pipeline, open this folder:**

```
reporting/
├── summary/
│   └── executive_summary.pdf     ← start here: all candidates on one-page cards
└── dossiers/
    └── {TICKER}.pdf              ← one per candidate: card + full analyst notes
```

The summary is the overview. Each dossier is a deep-dive for one name. Both are regenerated from the same source of truth so they never drift.

## What's in this repo

| Folder | What it is |
| --- | --- |
| `reporting/` | **Published outputs.** This is the only folder you open day-to-day. |
| `unified_system/` | The machinery — scanners, report generator, candidate source files, docs. |
| `unified_system/candidates/` | Source of truth: one `.md` per active candidate + `_curated_rationales.json` (the plain-English hypothesis/thesis/outcome/price/urgency data). |
| `unified_system/docs/` | How it works: `DECISIONS.md`, `INSTRUCTIONS.md`, `OBJECTIVES.md`, `SESSION_STATE.md`, plan docs. |
| `unified_system/tools/` | Python scripts. The main one is `report_generator.py`. |
| `_archive/` | Old projects and retired outputs — kept for reference, not active. |
| `Tool Audit 2026-04-14/` | Historical tool/connector audit. |

## Regenerating reports

From `unified_system/`:

```
python3 tools/report_generator.py --publish
```

This rewrites `reporting/summary/executive_summary.pdf` and one `reporting/dossiers/{TICKER}.pdf` per active candidate. Old dossiers for tickers that are no longer active are removed on each publish.

## Updating a candidate's thesis

1. Edit `unified_system/candidates/_curated_rationales.json` — this is where the hypothesis, thesis, price targets, urgency, and kill watch live.
2. (Optional) Edit the candidate's `.md` in `unified_system/candidates/{TICKER}_*.md` — this is the deeper background that becomes page 2+ of the dossier.
3. Re-run `--publish`.

## Glossary (shorthand used in reports)

- **PDUFA** — FDA decision date on a drug application.
- **CRL** — Complete Response Letter; FDA rejects or defers approval.
- **13D / 13D/A** — SEC filings disclosing an activist stake (≥5%) / amendments to it.
- **Poison pill** — board defense that dilutes shareholders who exceed a threshold.
- **Merger arbitrage** — buy a target post-announcement, capture the spread to deal price.
- **Take-private** — public company acquired by private investors and delisted.
- **Urgency band** — VERY HIGH (hours-to-weeks) / HIGH (≤3 months) / MEDIUM-HIGH / MEDIUM / LOW.
