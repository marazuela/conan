# UI v2 — Checklist

Derived from `ui_v2/spec.md` (2026-04-21). Gates design-skill invocation on datapoint lock.

## Phase UI-v2.0 — Foundation (pre-design)

- [x] Extract architecture docx text to `ui_v2/requirements_source.md`.
- [x] Reconcile docx against live Supabase schema (spec.md Appendix A) and current Phase 4 dashboard routes.
- [x] Draft `ui_v2/datapoints.md` — per-entity field catalog with keep/drop/defer column.
- [x] Draft `ui_v2/spec.md` — goals, user journeys, anti-goals, phased delivery.
- [ ] **Pedro review — datapoints.md.** Mark keep/drop/defer per row. Return notes on §13 data-model gaps.
- [ ] **Pedro review — spec.md.** Confirm goals (G1–G6), anti-goals (AG1–AG6), phase split (v2.1 → v2.4).
- [ ] Resolve §13 spec-level data-model decisions (liquidity floor field, scanner prose storage, archive→pre-edge linkage, decisions register surfacing, Q-number convention on operator_flags).
- [ ] Invoke `design:design-system` to define primitives (type scale, color, spacing, tokens) consistent with zinc-950 + Geist Mono + green-400.
- [ ] Invoke `design:design-critique` on the first-phase screen set.
- [ ] Approve design proposal before any implementation starts.

## Phase UI-v2.1 — Rubric transparency (ships G1)

- [ ] Static content bundle at `dashboard/content/profiles/*.md` (6 profile explainers from docx §5).
- [ ] `/profiles` route — list view of 6 profiles with dimensions + weights + band thresholds.
- [ ] `/profiles/[name]` route — single-profile rubric detail with auto-cap rules in plain language.
- [ ] Enriched `/signals/[id]` — dimension breakdown card, auto-cap explainers linking to `/profiles/[name]`, rubric_version badge.
- [ ] Verify: open a signal from each profile, confirm rubric card renders with correct weights and auto-cap rule explanations.

## Phase UI-v2.2 — Primary-source traceability (ships G2)

- [ ] `/candidates/[id]` — linked signals panel (signals where entity_id matches, within candidate's active window).
- [ ] `/signals/[id]` — linked filings panel (join on source_content_hash).
- [ ] `/convergence` — member profiles mix, regions mix, explicit window width, classification tooltip.
- [ ] Verify: pick one active candidate, click through signals → filings → source URLs without leaving the app.

## Phase UI-v2.3 — Pre-edge narrative (ships G3, G4, archive post-mortem)

- [ ] Static content bundle at `dashboard/content/decisions/*.md` (D-003, D-005, D-008, D-013, D-014, D-018, D-047, D-052) or a minimal `decisions` ref table — decision pending in §9.
- [ ] Home-page pre-edge banner (AVNS reference, D-013 summary, active takeover_candidate + binary_catalyst counts, days since last Immediate-band pre-edge promotion).
- [ ] `/decisions` route — decisions register static view.
- [ ] `/archive` route — archived candidates with outcome, decision trail, and pre-edge signals that existed for misses (AVNS-style post-mortem).
- [ ] `/weekly` route — printable 2–5 opportunity shortlist mirroring the reportlab PDF content.
- [ ] Verify: AVNS archive page shows the pre-edge signals that existed before the 2026-04-14 announcement, linked to filings.

## Phase UI-v2.4 — Operational depth (ships G5, G6)

- [ ] Static content bundle at `dashboard/content/scanners/*.md` — 17 scanner prose files from docx §4.
- [ ] `/scanners/[name]` route — scanner detail with rationale / edge / mechanics / candidate picker / profile mapping, plus latency trend from `scanner_runs`.
- [ ] `/alerts` route — Immediate-band dispatch inbox with Resend delivery status per recipient.
- [ ] Verify: `/scanners/edgar_filing_monitor` shows the EFTS full-text search mechanics and the 35s budget (D-018) with latest probe latency.

## Out of UI v2 scope (Phase 5+)

- Thesis authoring / review queue actions.
- Annotations editor.
- Watchlist CRUD.
- Notification preferences editor.
- Admin UI for rubrics / scanner config / PE allowlist.
- Mobile push, Slack/Teams integrations.

## Working conventions (carry from tasks/todo.md + PRD §12)

- Plan mode before non-trivial step. `tasks/ui_v2_todo.md` is the working plan.
- Verify before done — every phase exits with proof (route walkthrough, screenshot, or smoke curl).
- After any Pedro correction, update `tasks/lessons.md`.
- Elegance check before surfacing non-trivial work.
