# UI v2 — Scoped Requirements

**Status:** draft for Pedro review — pairs with `ui_v2/datapoints.md` (detailed field catalog). Lock both before invoking design skills.
**Date:** 2026-04-21.
**Builds on:** the Phase 4 dashboard (deployed at `conan-dashboard.vercel.app`, 11 routes) and the architecture narrative in `Unified_Investment_Research_System_Report (1).docx` (2026-04-20).
**Target repo:** `/Users/Pico/Documents/Claude/Projects/Conan/dashboard/` (Next.js 16 / React 19 / Supabase / shadcn / Tailwind v4 on Vercel).

## 1. What UI v2 is (and is not)

UI v2 is an extension of Phase 4, not a rewrite. Phase 4 proved the scaffolding — auth, proxy, SSR, realtime, storage — works. UI v2 is about surfacing more of what the system already does, grounded in the architecture docx, so Pedro and collaborators can operate the system without reading markdown dossiers or the PDF report.

UI v2 is **not** a new backend, not a new data model (except where `ui_v2/datapoints.md` §13 flags specific gaps), not a swap of framework, and not design-driven. The design skills run after this spec + the datapoint catalog are locked.

## 2. Why now

Three forcing functions:

**The AVNS miss.** The system had pre-edge signal on Avanos six days before the American Industrial Partners take-private announcement and did not promote. The docx §1 names this the architectural inflection that drove D-013 (pre-edge mandate) and the two new scanners. The current dashboard does not make the pre-edge mandate visible anywhere — no banner, no archive post-mortem, no "what signal existed before the edge went public" view. UI v2 closes that.

**The rubric is invisible.** The 6 scoring profiles with their dimensions and weights are the analytical core of the system. Today a signal shows a score and a band but not which dimensions contributed, which auto-caps fired, or what version of the rubric scored it. Making the rubric visible is a trust upgrade and a debugging aid in equal measure.

**Primary-source traceability is incomplete.** Docx §2.3 is emphatic that every claim must cite a filing URL. Phase 4 shows `source_url` on the signal detail, but candidate → filings is not linked in the UI and the archive view doesn't show which filings triggered which outcome. UI v2 closes that loop.

## 3. Primary user and journeys

Primary user: Pedro. Secondary: 2–3 collaborators on the same workspace (per PRD §4).

Six journeys UI v2 optimizes for, in declining frequency:

**J1 — Morning triage.** Pedro opens `/`, sees the pre-edge banner, the weekly shortlist, any open flags, and the last 24h signal volume by band. Expected time on page: under 60 seconds.

**J2 — Immediate-band follow-up.** Pedro receives an alert email with a signal deep link. Clicks, lands on `/signals/[id]`, reviews the rubric breakdown, confirms primary source, and either promotes to watchlist mentally or opens the linked filings.

**J3 — Weekly shortlist prep.** Pedro opens `/weekly`, prints or shares the 2–5 opportunity view, optionally annotates.

**J4 — Convergence investigation.** A multi-profile convergence triggers interest. Pedro opens `/convergence`, drills into the group, sees the regional + profile mix, window width, and the member signals.

**J5 — Scanner health check.** When volumes look off or an auth blocker is suspected, Pedro opens `/scanners` to spot the yellow/red cards, drills into `/scanners/[name]` for latency history and the prose-description of what that scanner does.

**J6 — Archive post-mortem.** A miss lands (or a win). Pedro opens `/archive`, picks the ticker, and sees: thesis that got shipped, outcome, and — crucially — the signals that existed pre-edge and did not promote. Feeds back into rubric / scanner tuning.

## 4. Goals

**G1.** Every score in the UI is decomposable — a user can click through to see which dimensions contributed, which auto-caps fired, and which rubric version was used.

**G2.** Every candidate in the UI is primary-source-traceable — one click to the signals that fed it, one click from each signal to the filing URL.

**G3.** The pre-edge mandate is visible from the home page. AVNS + D-013 are surfaced as context, not buried in DECISIONS.md.

**G4.** The weekly shortlist (the explicit 2–5 opportunities per week deliverable) has its own URL, not just a PDF in Storage.

**G5.** Scanner health is more than a color chip — each scanner has a detail page with rationale, edge, mechanics, and latency trend.

**G6.** Alerts have an inbox. Pedro can see the last 50 Immediate-band dispatches with delivery status, not just whatever landed in his email.

## 5. Anti-goals

**AG1.** No authoring UI for theses. PRD §2 + §5 are explicit — Claude drafts, users approve. Phase 5 review queue stays read-first.

**AG2.** No admin UI for rubrics, scanner config, or PE allowlist. Edit in Supabase Studio. PRD §5.

**AG3.** No back-testing, no market commentary, no news feed. Anti-goals from `docs/OBJECTIVES.md` carry forward.

**AG4.** No replacing reportlab. The weekly PDF continues to ship from the reporting Modal job; `/weekly` is an additional surface, not a replacement.

**AG5.** No mobile push, no Slack/Teams, no multi-tenant. PRD §14.

**AG6.** No changes to scoring logic, rubric weights, or auto-cap rules. UI v2 exposes them; it does not change them.

## 6. Data source strategy

Supabase is the single read source for every screen. Server components read directly; client components subscribe to Realtime where live updates matter (`/signals`, `/candidates` review queue, `/alerts`). Storage provides signed URLs for `filings/`, `reports/`, and email bodies.

The docx is not a data source for the UI. Where the UI needs the architectural narrative (scanner rationale, decisions register, pre-edge explainer), the content ships as static markdown in the dashboard repo at `dashboard/content/` and is imported at build time. This keeps the dashboard deployable independently of DB state.

## 6b. Design tokens — the baseline the design skill extends

The aesthetic baseline is already set by `dashboard/app/globals.css`. The `design:design-system` invocation in §11 step 4 must treat this as ground truth and propose *additions* on top (scoring-band accents, profile accents, convergence chip). No token in the table below may be redefined without an explicit decision note.

**Font stack.** `--font-sans` (default, inherited from the Next.js layout) and `--font-geist-mono` (data, numerics, signal_id, FIGI, accession numbers). `--font-heading` aliases `--font-sans`. Mono is already the default for `font-mono` via `@theme inline`.

**Radius.** `--radius: 0.625rem` base. Derived: `--radius-sm` (×0.6), `--radius-md` (×0.8), `--radius-lg` (×1.0), `--radius-xl` (×1.4), `--radius-2xl` (×1.8), `--radius-3xl` (×2.2), `--radius-4xl` (×2.6).

**Color tokens.** Both `:root` (light) and `.dark` (dark) define the same surface set. Palette is greyscale OKLCH (chroma = 0) except two accents:

| Token | `:root` (light) | `.dark` | Use |
|---|---|---|---|
| `--background` | `oklch(1 0 0)` | `oklch(0.145 0 0)` | page surface |
| `--foreground` | `oklch(0.145 0 0)` | `oklch(0.985 0 0)` | body text |
| `--card` | `oklch(1 0 0)` | `oklch(0.205 0 0)` | panel surface |
| `--card-foreground` | `oklch(0.145 0 0)` | `oklch(0.985 0 0)` | panel text |
| `--popover` / `--popover-foreground` | matches card | matches card | dropdowns, tooltips |
| `--primary` | `oklch(0.205 0 0)` | `oklch(0.922 0 0)` | buttons, emphasis |
| `--primary-foreground` | `oklch(0.985 0 0)` | `oklch(0.205 0 0)` | on-primary text |
| `--secondary` / `--muted` / `--accent` | `oklch(0.97 0 0)` | `oklch(0.269 0 0)` | secondary surfaces |
| `--secondary-foreground` / `--accent-foreground` | `oklch(0.205 0 0)` | `oklch(0.985 0 0)` | on-secondary text |
| `--muted-foreground` | `oklch(0.556 0 0)` | `oklch(0.708 0 0)` | caption text |
| `--destructive` | `oklch(0.577 0.245 27.325)` (red) | `oklch(0.704 0.191 22.216)` (red) | errors, discard band, auto-cap trips |
| `--border` | `oklch(0.922 0 0)` | `oklch(1 0 0 / 10%)` | hairlines |
| `--input` | `oklch(0.922 0 0)` | `oklch(1 0 0 / 15%)` | form fields |
| `--ring` | `oklch(0.708 0 0)` | `oklch(0.556 0 0)` | focus rings (keep per §6g) |
| `--chart-1 … --chart-5` | greyscale gradient 0.87 → 0.269 | same | default chart series |
| `--sidebar-primary` | `oklch(0.205 0 0)` | `oklch(0.488 0.243 264.376)` (blue-violet, hue 264 — the only chromatic accent in dark mode) | sidebar accent |
| `--sidebar-*` (other) | greyscale matching `:root` primary/accent set | greyscale matching `.dark` muted set | sidebar chrome |

**What the design skill must add** (not in the table today):

- **Band tokens** — four semantic colors mapped to the four bands (`immediate`, `watchlist`, `archive`, `discard`). Must be AA-compliant against both modes. `discard` aliases `--destructive`.
- **Profile tokens** — six semantic colors for the six scoring profiles. Used on chips, profile-detail pages, and the convergence-group "member profiles mix" pill (datapoints §3).
- **Convergence-bonus token** — one accent for the +5 / +10 convergence chip; distinct from the profile tokens.
- **Source-freshness token** — opacity ramp or hue shift for "edge decay" (docx §5.6 dimension), surfaced on signal rows as recency.

All additions must be defined as CSS custom properties in both `:root` and `.dark`, mirroring the existing pattern.

## 6c. Content bundle layout — `dashboard/content/`

Static markdown shipped with the dashboard build. Imported via `next/mdx` or a server-side reader; never fetched at runtime.

```
dashboard/content/
├── scanners/<scanner_name>.md        # one per scanner (17 files). Frontmatter: name, region, cadence, default_profile, version, known_issues (array of Q-numbers). Body: rationale, edge, data_source, mechanics, candidate_picker, profile_mapping — the five docx §4 headings.
├── decisions/<d_id>.md                # one per load-bearing decision (D-003, D-005, D-008, D-013, D-014, D-018, D-047, D-052). Frontmatter: id, title, driver, effective_date, related_docx_sections. Body: the decision narrative.
├── rubric_rules/<profile>/<rule_id>.md   # one per auto-cap rule. Frontmatter: profile, rule_id (matches signals.auto_caps_triggered entry), band_effect (immediate→watchlist | discard | watchlist). Body: the human-readable condition, e.g. "annualized return < risk-free rate + 3%."
├── profiles/<profile_name>.md         # one per scoring profile (6 files). Frontmatter: name, max_score, band_thresholds, rubric_version_seeded_at. Body: the profile narrative from docx §5.1–§5.6.
├── pre_edge_explainer.md              # home-page banner long-form (AVNS miss + D-013 mandate). Frontmatter: title, last_reviewed.
└── known_issues/<Q_id>.md             # Q-016, Q-017, Q-018, Q-019. Frontmatter: q_id, severity, scanner (nullable). Body: what's blocked, what resolving it unlocks.
```

Frontmatter uses YAML. A shared TypeScript type (`dashboard/lib/content/types.ts`) defines the schema per folder; build fails if any markdown file's frontmatter violates its schema. This unblocks phase UI-v2.0 (content ships with no UI change).

## 6d. Realtime subscription strategy

Supabase Realtime per route. One channel per route, tied to the route's component lifecycle. Unsubscribe on `beforeunload` and on route change.

| Surface | Table | Filter (Realtime postgres_changes) | Polling fallback |
|---|---|---|---|
| `/signals` | `signals` | `event=INSERT`, ordered by `scan_date DESC`, paginated 50/page | 60s |
| `/candidates` review queue | `candidate_events` | `event=INSERT`, `event_type IN (thesis_drafted_by_claude, thesis_approved_by_user, gate_rejected)` | 90s |
| `/alerts` | `alert_deliveries` | `event=INSERT` OR `event=UPDATE`, `dispatched_at > now() - 7d` | 60s |
| `/flags` | `operator_flags` | `event=INSERT` or `event=UPDATE`, `resolved_at IS NULL` | 120s |
| `/` home | `signals` (band=immediate) + `operator_flags` (unresolved) | composite subscription | 60s |

Polling fallback activates when the websocket disconnects for >10s. No optimistic UI writes — v2 is read-only.

## 6e. Deep-link URL contract

Every email dispatch, PDF footer, and weekly-report link must emit URLs matching these canonical shapes. Changing these shapes invalidates archived emails/PDFs, so they are frozen once this spec locks.

| URL | Parameter | Source column |
|---|---|---|
| `/signals/[signal_id]` | UUID | `signals.signal_id` |
| `/candidates/[ticker_mic]` | lowercase `{ticker}-{mic}` composite (e.g. `/candidates/avns-xnys`) | `candidates.(ticker, mic)` |
| `/alerts/[alert_id]` | UUID | `alerts.id` |
| `/convergence/[convergence_key]` | string, no slashes | `signals.convergence_key` |
| `/profiles/[profile_name]` | one of the six profile slugs | `rubrics.profile` |
| `/scanners/[name]` | scanner_name, no slashes | `scanners.name` |
| `/decisions/[d_id]` | `D-NNN` slug | `dashboard/content/decisions/` filename |
| `/entities/[figi]` | FIGI (uppercase) | `entities.issuer_figi` |
| `/archive/[ticker_mic]` | same shape as `/candidates/[ticker_mic]` | `candidates` where `state IN (delivered, killed)` |

The Resend email template (PRD §11) and the reportlab PDF footer consume these. The dashboard never rewrites these paths.

## 6f. Print & export posture

`/weekly` must ship with `@media print` CSS — single-page layout matching US-letter dimensions (8.5" × 11") so printing from the browser produces the same artifact shape as the reportlab PDF.

**Canonicity decision.** The `/weekly` URL is the canonical weekly record for the *current* week: it reads live state and updates as the shortlist evolves mid-week. The reportlab PDF is the archival snapshot — one per week, written to Storage at `reports/YYYY/WW/weekly.pdf` each Friday. Email distribution attaches the PDF (PRD §11 / reporting §6.8). Rationale: signals roll out of the 14-day window, so the URL cannot serve as an audit record — the PDF is the immutable weekly freeze. AG4 is preserved: reportlab stays, `/weekly` is additive.

No CSV export in v2. No "copy to clipboard" on data tables. Export discipline follows the same rule as admin UI (AG2): if you need the raw rows, Supabase Studio.

## 6g. Accessibility target

WCAG 2.1 AA. Non-negotiable:

- Keyboard-only navigation on `/signals` list, `/candidates` queue, `/alerts` inbox. Tab order matches visual order. `Enter` opens the detail view. `Escape` closes any drawer.
- Focus rings preserved from shadcn defaults — the `--ring` token is never overridden at the component level.
- Color-contrast check during `design:design-system` pass: every band token, profile token, and convergence chip passes AA against both `--background` and `--card` in both `:root` and `.dark`.
- Screen-reader labels on every band chip, auto-cap chip, profile chip, and scanner-status chip. No color-only semantics.
- `/signals` virtualized lists: arrow keys navigate, `aria-rowcount` and `aria-rowindex` correct even under virtualization.

No axe-core CI yet; a one-shot axe run during `design:design-critique` is the gate.

## 6h. Responsive posture

Desktop-first. Minimum supported viewport: 1024px × 720px. Below 1024px, the app renders a notice card with the text: "Dashboard is desktop-only. On mobile, check your email alerts and the weekly PDF." Links to the last weekly PDF (signed URL) and the email inbox.

Rationale: Pedro + 2–3 collaborators; morning triage is a desktop habit per J1; the docx has no mobile views. AG5 already rules out mobile push; this extends consistently to the UI. Revisiting mobile is Phase 6+ scope.

## 6i. Color-mode posture

Both `:root` (light) and `.dark` token sets exist in `globals.css`. **Default: dark**, matching data-tool convention and the research-session environment (monitors, long sessions, low ambient light). A persistent toggle in the sidebar switches to light. The preference persists per-user in `localStorage` (key: `conan-color-mode`). No `prefers-color-scheme` follow — deterministic per session, so a screen-share or paired-session view is identical regardless of OS setting.

Both modes must pass the AA contrast check in §6g. Every new token added under §6b must define both values; single-mode tokens are a build error.

## 7. Scope — what ships in UI v2

Listed in `ui_v2/datapoints.md` §12. Summary:

**Extended from v1:** `/`, `/signals`, `/signals/[id]`, `/convergence`, `/candidates`, `/candidates/[id]`.

**New:** `/weekly`, `/archive`, `/scanners/[name]`, `/profiles`, `/profiles/[name]`, `/alerts`, `/decisions`.

**Kept as-is from v1:** `/scanners` (card grid), `/flags`, `/reports`, auth flow.

## 8. Out of scope for UI v2

`annotations` editing (Phase 5), watchlists CRUD, notification preferences editor, candidate review queue **actions** (Phase 5 scope per the Phase 4 todo). The thesis_writer pipeline and `candidate_aging` outputs will be rendered as they become available but the wiring is Phase 5, not Phase 4.1.

**Review queue read/write boundary (explicit).** The v2 review queue is **read-only**. It renders the `candidate_events` timeline (`thesis_drafted_by_claude → gate_rejected | thesis_approved_by_user`) and a dossier preview. Approve / reject / annotate buttons are Phase 5. This matches PRD §13 — "users review, annotate, or reject via the dashboard (not by editing markdown files)" — by staging the interactive surface to Phase 5 while building the read view in v2. The read view also surfaces `gate_rejected` events with the rejection reason (datapoints §2 "Gate rejection reason" row) so the queue explains why signals did *not* become candidates, without crossing into AG1 (thesis authoring).

## 9. Data-model gaps to surface

See `ui_v2/datapoints.md` §13. In short: liquidity-floor verification field, scanner prose storage, archive → pre-edge signal linkage semantics, decisions register surfacing strategy, Q-number convention on `operator_flags`. These are spec-level decisions that must be made before implementation; none are UI design decisions.

## 10. Phased delivery

Every new route in phases v2.1–v2.4 ships behind a `NEXT_PUBLIC_UI_V2_<slug>` env var (default `false`). Enabling a flag surfaces the route in the sidebar and in links; disabling it removes both the route and any cross-links cleanly. This lets phases ship independently and lets a regression be rolled back by env flip, with no code revert.

**Phase UI-v2.0 — Foundation (post spec/datapoint lock).** Static content bundle under `dashboard/content/` per §6c (scanners × 17, decisions × 8, rubric_rules, profiles × 6, pre_edge_explainer, known_issues × 4). No UI changes yet. Exit: `next build` passes with frontmatter-schema validation on every content file; content reads correctly from a build-time importer in a spike route gated to `?preview=content`.

**Phase UI-v2.1 — Rubric transparency.** `/profiles`, `/profiles/[name]`, enriched `/signals/[id]` with dimension breakdown + auto-cap explainers (links to `rubric_rules/` content), + `cmd+k` global search (D2). Ships G1. Exit: Pedro opens any Immediate-band `/signals/[id]` and can name the top-weighted contributing dimension and every triggered auto-cap without scrolling past the rubric card (≤ 800px viewport-height budget for the card).

**Phase UI-v2.2 — Primary-source traceability.** Linked-signals panel on `/candidates/[id]`, linked-filings panel on `/signals/[id]`, enriched convergence view, + `/entities/[figi]` per-issuer timeline (D1). Ships G2. Exit: Pedro opens a candidate detail, reaches the underlying filing URL in ≤ 2 clicks, and from a convergence group reaches every member signal's source filing in ≤ 2 clicks per member.

**Phase UI-v2.3 — Pre-edge narrative.** Home-page banner, `/decisions`, `/archive`, `/weekly` (with the print CSS per §6f). Ships G3, G4, and archive post-mortem. Exit: Pedro opens `/` in the morning and the pre-edge banner renders the active-pre-edge-signal count and days-since-last-pre-edge-promotion without a network round-trip beyond the initial SSR; `/archive/avns-xnys` shows the pre-edge signals that existed on AVNS before 2026-04-14 and the reason none promoted.

**Phase UI-v2.4 — Operational depth.** `/scanners/[name]`, `/alerts` (two-tab layout per datapoints §6 / C4). Ships G5 and G6. Exit: when Q-017 auth-required status fires, Pedro reaches the Q-017 explainer (`dashboard/content/known_issues/Q-017.md`) from the scanner card in one click; `/alerts` renders the last 50 dispatches + the Pending AI Review tab with thesis_writer 15/day budget state visible.

## 11. What we do next

Pedro is the sole reviewer per PRD §4. Row-level keep / drop / defer marks in `datapoints.md` are final once Pedro signs off; no collaborator overrides.

1. Pedro reviews `ui_v2/datapoints.md` and marks keep / drop / defer per row.
2. Pedro reviews this spec and confirms the phase split.
3. Any §9 spec-level decisions get resolved (or deferred with a dated note).
4. Invoke `design:design-system` to define the primitives (type scale, spacing, scoring-band and profile accents, convergence-bonus chip, rubric-card layout) as *extensions* of the token baseline enumerated in §6b. The baseline is greyscale OKLCH + Geist Mono; the skill adds semantic color for bands/profiles/convergence without reskinning the chrome.
5. Invoke `design:design-critique` on the first-phase screens.
6. Implementation starts only after steps 4–5 complete.

This spec does not authorize implementation. It authorizes the datapoint conversation.
