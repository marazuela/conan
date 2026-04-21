# UI v2 — Datapoint Catalog

**Status:** draft for Pedro review — lock before invoking `design:design-system` / `design:design-critique`.
**Source of record:** `Unified_Investment_Research_System_Report (1).docx` (2026-04-20 architecture narrative) + `spec.md` Appendix A (Supabase column-level schema) + current Phase 4 dashboard at `conan-dashboard.vercel.app`.
**Stack assumed:** Next.js 16 + React 19 + Supabase (SSR + Realtime) + shadcn/ui + Tailwind v4 on Vercel. UI v2 extends the Phase 4 dashboard; it is not a rewrite.

## Purpose of this document

Every row below names one datapoint the UI v2 could surface. For each: the source column (or computed expression), whether it's already rendered in Phase 4, and the argument for (or against) surfacing it in v2. The design skills should not start until Pedro has marked **keep / drop / defer** against each row so the downstream layout work has a bounded target.

Legend: `[v1]` = already in Phase 4 dashboard. `[new]` = proposed for v2. `[gap]` = in docx but no corresponding Supabase column yet — flagged for spec update. `[defer]` = in scope of Phase 5+ per existing plan, listed here for completeness.

---

## 1. Entity: Signal

Supabase table `signals`. Section 4 of the docx. The 733-row rolling log.

| Field | Source | In v1? | Rationale for v2 |
|---|---|---|---|
| `signal_id` | `signals.signal_id` | [v1] | Anchor for deep links (already used in email template URLs). |
| Band | `band_with_bonus` (enum immediate/watchlist/archive/discard) | [v1] | Primary sort + color key. |
| Score | `score_with_bonus` | [v1] | Headline number. |
| Pre-bonus score | `score` | [new] | Show alongside `score_with_bonus` when `convergence_bonus > 0` so the +5/+10 contribution is visible. Docx §6.3. |
| Convergence bonus | `convergence_bonus` (0/5/10) | [v1] | Already shown; v2 should make it a first-class chip. |
| Convergence key | `convergence_key` | [v1] | Link target to `/convergence`. |
| Scanner name + version | `scanners.name` + `scanners.tool_path` | [v1] | Already shown as name; tool version would be new. |
| Scoring profile | `scoring_profile` (1 of 6) | [v1] | Primary filter. |
| Signal type | `signal_type` (e.g. "SC 13D", "PDUFA", "MBO") | [v1] | Filterable secondary key; docx routes `signal_type → profile` per scanner. |
| Thesis direction | `thesis_direction` (long/short/neutral) | [v1] | Drives convergence classification. |
| Scan date / source date | `scan_date`, `source_date` | [v1] | "Edge freshness" — docx §5.6 dimension. |
| Issuer ticker + MIC | `entities.primary_ticker` + `primary_mic` | [v1] | Universal label. |
| Issuer FIGI | `issuer_figi` | [new] | Show on detail page. Docx §10 calls out FIGI as the cross-scanner primary key; surfacing it builds trust. |
| Fallback id used | `entity_identifiers` row that resolved | [new] | When FIGI is missing and `codigo_cvm` / `id_empresa_biva` / `stock_code` / name-norm carried the resolution, show it. Docx §6.1 + §10.2. |
| Market cap (USD at ingest) | `entities.market_cap_usd` + `market_cap_as_of` | [new] | Confirms the $215M floor was cleared — currently implied, never shown. |
| Dimensions (per-profile) | `signals.dimensions` JSONB | [v1] | Already on detail page; v2 can present it as a scored-rubric card (see §5). |
| Auto-caps triggered | `auto_caps_triggered` text[] | [v1] | Already shown; v2 should link each `rule_id` to a rubric explainer (e.g. "A: return < RFR + 3%"). |
| Rubric version | `rubric_version_id` | [new] | Replayability per PRD §5. Small badge on detail page. |
| Source URL | `signals.source_url` | [v1] | Primary-source traceability — docx §2.3 is emphatic about this. |
| Source content hash | `source_content_hash` | [new] | Joins signal → `filings` row; useful for dedup explanation. |
| Filing type | joined `filings.filing_type` | [v1] | Already in detail. |
| Raw payload | `signals.raw_payload` JSONB | [v1] | Already in detail as JSON block. |
| Imported flag | `signals.imported` | [new] | Distinguishes historical-import (733 seed) from v2-native signals. PRD §11. Useful dim-label ("seeded"). |
| Keyword / excerpt | derived from `raw_payload.matched_keyword`, `raw_payload.excerpt` | [new] | Docx §4.1 edgar: EFTS full-text matches are the edge. Currently hidden in raw_payload. |

---

## 2. Entity: Candidate

Supabase tables `candidates`, `candidate_events`, `candidate_rationales`, `outcomes`. Section 7 of the docx. 5 active + 4 archived as of 2026-04-20.

| Field | Source | In v1? | Rationale for v2 |
|---|---|---|---|
| Ticker + MIC | `candidates.ticker` + `mic` | [v1] | Anchor. |
| State | `candidates.state` (watch/active/killed/delivered) | [v1] | Kanban lane in v1. v2 may replace Kanban with a review queue (Phase 5). |
| Scoring profile | `candidates.scoring_profile` | [v1] | Drives the rubric card. |
| Current score + band | `current_score`, `current_band` | [v1] | Headline number. |
| Thesis — situation | parsed from `dossier_markdown` or structured `extensions.thesis` | [v1] | Rendered via react-markdown in v1. D-008 min 80 chars. |
| Thesis — why underpriced | same | [v1] | D-008 min 100 chars. |
| Thesis — next catalyst + date | same; `next_catalyst_date` is a typed column (XOR `next_catalyst_window`) | [v1] | D-008 min 40 chars; ISO date required. |
| Thesis — kill conditions | `candidates.kill_conditions` JSONB array (structured) | [v1, partial] | v1 shows markdown dossier; v2 should render the structured JSONB array (each kill condition = one row with status new/armed/triggered). Phase 5 scope. |
| Position size target | [gap] | — | Docx §2.1 requires $3M+ liquidity floor per candidate. No explicit column; could live in `extensions` or as a computed check against `30d_ADV`. Flag for spec follow-up. |
| 30-day ADV | [gap] | — | Docx scoring dimensions reference "30-day ADV" for the liquidity dimension of every profile. Not in schema. Either computed client-side via a price-data feed (out of scope) or denormalized onto `entities`. Flag. |
| Dossier markdown | `candidates.dossier_markdown` | [v1] | Already rendered. |
| Rationale card (curated) | `candidate_rationales.one_liner/hypothesis/thesis/expected_outcome/price_targets/time_sensitivity/kill_watch/catalyst_date_iso` | [v1] | Header card on candidate detail. |
| Events timeline | `candidate_events` (created/state_changed/scored/thesis_drafted_by_claude/gate_rejected/…) | [v1] | Phase 5 extends with thesis_approved_by_user / annotation events. |
| Outcome | `outcomes.outcome_type` + `realized_return` | [v1, archive only] | Drives the archive post-mortem screen (see §8 below). |
| Thesis approved at | `candidates.thesis_approved_at` | [new] | Phase 5; make explicit on review queue. |
| Last aging evaluated | `candidates.last_aging_evaluated_at` | [new] | Staleness indicator on candidate card. |
| Annotations | `annotations.body` (per-user, RLS-scoped) | [defer] | Phase 5. |
| Linked signals | `signals` where `entity_id = candidates.entity_id` and within window | [new] | Today the UI doesn't show "which signals produced this candidate". Docx §6 convergence + §7 promotion both imply this link. Would be a major trust upgrade. |
| Linked filings | `filings` joined via `signals.source_content_hash` | [new] | Primary-source proof panel per candidate. Anchors the "every claim traceable to a filing URL" promise in docx §13. |
| Gate rejection reason | `candidate_events` where `event_type='gate_rejected'`, joined to the rejecting `signal_id` | [new] | When an Immediate-band signal exists on an issuer but no candidate was promoted, surface *why not* on `/signals/[id]` — e.g. "gate rejected: situation 40 chars (min 80)" or "catalyst XOR violated (both `next_catalyst_date` and `next_catalyst_window` set)." D-008 gate reasons live in `candidate_gate.py`; the UI reads the event body. Read-only observability; does not cross into AG1 (no authoring UI). |

---

## 3. Entity: Convergence Group

Computed view over `signals` grouped by `convergence_key`. Section 6 of the docx.

| Field | Source | In v1? | Rationale for v2 |
|---|---|---|---|
| `convergence_key` | grouping column | [v1] | Anchor. |
| Member count | count(*) over group | [v1] | Drives +5 / +10 bonus. |
| Bonus awarded | derived from count (2→+5, 3+→+10) | [v1] | Shown on each signal; make explicit as a group-level chip. |
| Classification | computed `same_direction` / `orthogonal` / `contradiction` / `neutral` from thesis_direction set | [v1] | Docx §6.3. Already labeled; v2 should explain what it means on hover. |
| Window width | 14d standard, 30d if any member is litigation | [new] | Implicit in data; show explicitly because docx §6.2 emphasizes the 14/30 asymmetry. |
| Winner signal | the highest-scoring member | [v1] | Already highlighted. |
| Member profiles mix | set of distinct `scoring_profile` values | [new] | Makes "orthogonal" visually obvious (e.g. activist_governance + binary_catalyst on same issuer). |
| Member regions mix | derived from `scanners.geography` | [new] | Docx §13 explicitly calls out multi-regulator / multi-jurisdiction convergence as a core edge. |
| Lead signal date / trailing signal date | min/max `scan_date` | [new] | Shows how the setup unfolded. |

---

## 4. Entity: Scanner

Supabase tables `scanners`, `scanner_runs`. Section 4 of the docx (17 scanners).

| Field | Source | In v1? | Rationale for v2 |
|---|---|---|---|
| Name | `scanners.name` | [v1] | Anchor. |
| Region / geography | `scanners.geography` | [v1] | Enables the docx's regional grouping (US / EU-UK / APAC / India-LatAm / Canada). |
| Cadence | `scanners.cadence` (3h/daily/weekly/on_demand) | [v1] | Already shown as label. |
| Default scoring profile | `default_scoring_profile` | [v1] | Context. |
| Signal-type → profile map | `signal_type_profile_map` JSONB | [new] | Docx §4 describes this routing per scanner. Currently invisible; on a scanner detail page this would be a small table ("SC 13D → activist_governance, DEFM14A → merger_arb, ..."). |
| Last run at / status / signals emitted | `last_run_utc`, `last_run_status`, `last_run_signals` | [v1] | Already shown on card. |
| Last probe latency | `last_probe_latency_ms` | [new] | Useful when diagnosing the 35s EDGAR budget (D-018). |
| Budget / timeouts | `timeout_soft_s`, `timeout_hard_s` | [new] | Show alongside latency so operator can see if a run was budget-bound. Docx D-014 + D-018. |
| Auth blocker | `last_run_status='auth_required'` + scanner name match for Q-017 (CourtListener), Q-019 (OpenDART) | [v1, partial] | v1 colors yellow/red; v2 could link the status chip directly to the Q-017 / Q-019 explainer. |
| Scanner rationale / edge / mechanics | `dashboard/content/scanners/<scanner_name>.md` (resolved via spec §6c) | [new] | Docx §4 writes one-page prose per scanner (rationale, edge, data source, mechanics, candidate picker, profile mapping). Shipped as static markdown in the dashboard repo, one file per scanner. No schema change. |
| Version | `scanners.tool_path` or a new `version` column | [new] | Docx labels scanners `edgar_filing_monitor (v2.4)`, `esma_short_scanner (v2.0)`, `fda_pdufa_pipeline (v2.0)`, `asx (rewritten 2026-04-20)`. |
| Known defect | [gap] | — | Docx Q-018 is a sedar_plus CLI defect. Could live in `operator_flags` with `source='scanner_probe'` scoped to that scanner. |

---

## 5. Entity: Rubric / Scoring Profile

Supabase table `rubrics`. Section 5 of the docx (6 profiles).

| Field | Source | In v1? | Rationale for v2 |
|---|---|---|---|
| Profile name | `rubrics.profile` | [v1, as label] | Anchor. |
| Rubric version | `rubrics.rubric_version` | [new] | Tie to `signals.rubric_version_id`. |
| Dimensions + weights | `dimension_weights` JSONB | [new] | Docx §5 lays out every profile's dimensions with weights (e.g. merger_arb: spread ×3, certainty ×2.5, annualized ×2, break_risk ×1.5, liquidity ×1). A dedicated profile page would let Pedro open any candidate score and see exactly why it got what it got. |
| Max score | derived (always 50) | [new] | Context. |
| Band thresholds | ≥35 / 25–34 / 15–24 / <15 constants | [new] | Docx §5 opening paragraph. Show as a legend on every score chip tooltip. |
| Auto-cap rules | static markdown in `dashboard/content/rubric_rules/<profile>/<rule_id>.md` — one file per rule, frontmatter binds `rule_id` to `signals.auto_caps_triggered` entries | [new] | Docx §5.1 Rule A, §5.1 Rule B, §5.3 EV floor, §5.5 party-resolution <0.92, §5.6 takeover rejection/going-concern. Surface each on the rubric page with the condition in plain language. Note: `rubrics` table has no `auto_caps` column — rule logic lives in `modal_workers/shared/rubric_engine.py::apply_auto_caps` (spec.md §7.1), and each rule writes a stable `rule_id` string. The content bundle is the display-layer explainer for those rule_ids; logic is not duplicated. |
| Effective at / superseded at | `effective_at`, `superseded_at` | [new] | For historical replay per PRD §5. |

**Special case — `takeover_candidate` (6th profile, new 2026-04-20):** the docx §5.6 + §4.6 is emphatic this is the AVNS-miss response. Surface the 5 setup patterns (PE take-private, streamlined-for-sale, strategic-review, insider+institutional, strategic-buyer-fit) and the triage gate explicitly.

**Special case — `phase3_base_rates` and `pe_filer_allowlist`:** both feed scoring dimensions but are currently invisible.

| `phase3_base_rates` | indication → approval probability | [new] | Show on binary_catalyst signal detail: "base rate for {indication}: 43%". |
| `pe_filer_allowlist` | 39 CIKs (Silver Lake, KKR, Apollo, Blackstone, Thoma Bravo, …) | [new] | Show on `takeover_candidate` signal detail: "PE filer match: Silver Lake". |

---

## 6. Entity: Alert

Supabase tables `alerts`, `alert_deliveries`. Section 8 + PRD §8 event flow.

| Field | Source | In v1? | Rationale for v2 |
|---|---|---|---|
| Alert id | `alerts.id` | — | — |
| Triggered signal | `alerts.signal_id` | — | Deep link. |
| Entity | `alerts.entity_id` | — | Label. |
| Dispatched at | `alerts.dispatched_at` | — | Confirms ≤5 min latency target. |
| Delivery status | `alert_deliveries.status` (queued/sent/failed/bounced) | — | Per-user / per-channel. |
| Resend message id | `alert_deliveries.resend_message_id` | — | Support. |
| Email body | `alerts.email_body_storage_path` | — | Optionally preview on alert detail. |

**UI v2 addition:** an `/alerts` inbox. Two tabs, because in v2 emails fire only after AI review + promotion to pre-edge — not on raw `alerts` INSERT (per the `email_alert_gating` design note, overrides the PRD §8 fan-out sketch):

- **Dispatched** — `alert_deliveries` rows, last 50 by `dispatched_at DESC`, with `status`, signal deep-link, and the resend message id.
- **Pending AI review** — signals at Immediate band that are waiting on a Claude-drafted thesis promotion (no `alerts` row yet). Column "Promotion state" reflects thesis_writer queue status so Pedro can see the bottleneck when the 15-drafts-per-day cap is reached. Cites `candidates/thesis_writer` routine (Immediate band, 15/day, Claude app routines API — users never author).

Neither tab permits action; both are diagnostic.

---

## 7. Entity: Operator Flag

Supabase table `operator_flags` (schema pending per Phase 4 commit 6). Replaces v1's `OPEN_QUESTIONS.md`. Docx §11.

| Field | Source | In v1? | Rationale for v2 |
|---|---|---|---|
| Severity | `severity` (info/warn/critical) | [v1] | Filter. |
| Source | `source` (translation_health / scanner_probe / convergence_qa / candidate_aging / thesis_writer / reactor / reporting_weekly / litigation_baselines / manual) | [v1] | Filter + icon. |
| Kind | `kind` | [v1] | Free-text kind tag. |
| Title / body | `title`, `body` | [v1] | Display. |
| Evidence | `evidence` JSONB | [v1] | Pretty-printed on detail. |
| Scoped to | scanner / entity / signal / candidate FKs | [v1] | Cross-link to the affected entity. |
| Resolved | `resolved_at`, `resolved_by`, `resolved_note` | [v1] | Phase 4 has resolve form. |
| Docx Q-numbers | [gap] | — | Q-016 / Q-017 / Q-018 / Q-019 are named blockers in the docx. Propose a `kind='known_issue'` convention so these four get a stable card each and don't churn with routine probe alerts. |

---

## 8. Entity: Archive & Post-Mortem

Not a table — a view. Section 1 + §7.2 of the docx. TVTX (WIN), AVNS (MISS), GSAT (WIN), SEM (NEUTRAL).

| Field | Source | In v1? | Rationale for v2 |
|---|---|---|---|
| Archived candidate list | `candidates` where `state='delivered'` or `'killed'` | [v1, kanban lane] | — |
| Outcome tag | `outcomes.outcome_type` + `realized_return` | [v1, detail] | — |
| Pre-edge signal that existed | signals where `entity_id = X` and `scan_date < announce_date` | [new] | **This is the core AVNS lesson** — docx §1 + §2.2 + §4.6. For every archived miss, show what primary-source signal the system had pre-edge, and why it didn't promote. Would let Pedro learn from misses without cross-referencing files. |
| Decision trail | `candidate_events` filtered to the case | [new] | Timeline of what the system did and when. |
| Related decisions | docx §12: D-013 was driven by AVNS | [new] | Link archive to the decisions register. |

---

## 9. Cross-cutting: Decisions Register

Docx §12. D-003, D-005, D-008, D-013, D-014, D-018, D-047, D-052 are load-bearing. Currently in `docs/DECISIONS.md`, not in the database (PRD §3 explicit non-goal to migrate).

| Field | Source | In v1? | Rationale for v2 |
|---|---|---|---|
| Decision ID + summary | Static markdown loaded at build time | [new] | A `/decisions` route or a right-rail drawer. Let auto-cap chips, convergence fallbacks, and the pre-edge disqualifier link to the relevant decision. Keeps the "why" visible without leaving the UI. |

---

## 10. Cross-cutting: Weekly Shortlist

Docx §2.1 — the explicit deliverable is 2 to 5 opportunities per week. Currently synthesized in the weekly reportlab PDF (unified-reporting). Not in the dashboard.

| Field | Source | In v1? | Rationale for v2 |
|---|---|---|---|
| This-week shortlist | `candidates` where `state IN ('active','watch')` ordered by `current_score DESC LIMIT 5` | [new] | A `/weekly` route (not a tab on home — see §12). Single printable view with each candidate's thesis / catalyst / kill conditions / sizing. Mirrors the PDF so Pedro can share a URL; the PDF remains the archival snapshot (spec §6f). **Empty-state rule:** docx §2.1 promises 2–5 candidates weekly; if fewer than 2 qualify (`state IN ('active','watch')` with thesis_approved_at NOT NULL), the page renders "No shortlist this week" + the top 3 active candidates by `current_score` as a diagnostic block so an empty week is never a blank page. |

---

## 11. Cross-cutting: Pre-Edge Banner

Docx §1 + §2.2 — the organizing principle of the current architecture. Not surfaced anywhere in UI v1.

Proposed: a permanent home-page card summarizing (a) AVNS miss reference, (b) D-013 pre-edge mandate, (c) count of active takeover_candidate and binary_catalyst signals, (d) days since last Immediate-band pre-edge promotion. Anchors every session in the mandate.

**Metric definitions (locked):**

- **(c) Active pre-edge signal count** = `count(signals)` where `band_with_bonus='immediate'`, `scoring_profile IN ('takeover_candidate', 'binary_catalyst')`, and `scan_date > now() - 14 days`. Excludes `pre_phase3_readout` only because pre_phase3 signals route through the `binary_catalyst` profile (docx §4.6).
- **(d) Days since last pre-edge promotion** = `EXTRACT(EPOCH FROM (now() - max(candidate_events.created_at))) / 86400` where `event_type='created'` and the candidate's `scoring_profile IN ('takeover_candidate', 'binary_catalyst')`. Uses `event_type='created'` explicitly — `'promoted'` is *not* a valid `candidate_events.event_type` value (CHECK constraint). If no rows qualify, render "never since 2026-04-20 launch" instead of infinity.

Body copy for the banner ships from `dashboard/content/pre_edge_explainer.md` (spec §6c) so the narrative can be edited without a deploy.

---

## 12. Proposed v2 screen inventory

Not design — just which surfaces exist and what they load. For Pedro to accept/edit before `design:design-system`.

| Route | Purpose | New vs existing |
|---|---|---|
| `/` | Home — KPIs + pre-edge banner + weekly shortlist + open flags | extend v1 |
| `/weekly` | Printable 2–5 opportunity shortlist | **new** |
| `/signals` | Rolling log (filterable) + realtime banner | keep v1 |
| `/signals/[id]` | Signal detail with rubric breakdown, base-rate citation, PE allowlist match, linked filings | extend v1 |
| `/convergence` | Convergence groups with classification + window width + regions mix | extend v1 |
| `/candidates` | Review queue (Phase 5 replaces Kanban) + linked signals panel | extend v1 |
| `/candidates/[id]` | Dossier + structured kill conditions + linked signals + linked filings + annotations (Phase 5) | extend v1 |
| `/archive` | Archived candidates with outcome + pre-edge signal that existed | **new** |
| `/scanners` | Scanner card grid | keep v1 |
| `/scanners/[name]` | Scanner detail — rationale/edge/mechanics prose + signal-type routing + latency history | **new** |
| `/profiles` | 6 profiles with dimensions/weights/auto-caps + band thresholds | **new** |
| `/profiles/[name]` | Single profile rubric detail | **new** |
| `/alerts` | Immediate-band dispatch inbox — two tabs: Dispatched + Pending AI review (§6 C4) | **new** |
| `/flags` | Operator flags (Phase 4 ok) | keep v1 |
| `/decisions` | Decisions register static view | **new** |
| `/reports` | Storage listing (Phase 4 ok) | keep v1 |
| `/entities/[figi]` | Per-issuer timeline: market cap, all signals (14/30d window), convergence memberships, linked + archived candidates. FIGI is the cross-scanner primary key per docx §10. Low build cost, high value for J4. | **new (v2.2)** |
| `cmd+k` global search | Ticker / FIGI / entity-name / signal_id / convergence_key jump. shadcn `<Command>` primitive. Not a route — a palette overlay mounted at app root. | **new (v2.1)** |
| `/theses` (deferred) | Claude-drafted thesis inbox with 15/day budget burndown, draft → approved lead time, rejections. Subsumed by the Phase 5 review-queue build-out; flagging here to prevent duplicate-UX drift. | **deferred → Phase 5** |

---

## 13. Data-model gaps to surface upstream

Flagging these for `spec.md` follow-up rather than inventing UI state:

1. **Per-candidate liquidity check** — docx §2.1 specifies $3M+ tradable liquidity floor. No column exists. Options: denormalize `30d_adv_usd` onto `candidates`, or computed at report time from a price-data feed. **Decision owner: Pedro (per spec §11).** Required before `/weekly` and `/candidates/[id]` can display pass/fail on the liquidity dimension.
2. **Scanner prose description** — docx §4 writes rich per-scanner narrative (rationale, edge, data source, mechanics, candidate picker, profile mapping). Not in DB. **Resolved (ui_v2 spec §6c):** static markdown shipped in `dashboard/content/scanners/<scanner_name>.md` with frontmatter (name, region, cadence, default_profile, version, known_issues) and the five docx §4 headings as body. No schema change; no runtime fetch. Same pattern covers `decisions/`, `rubric_rules/`, `profiles/`, `known_issues/`, and `pre_edge_explainer.md`.
3. **Archive → pre-edge signal linkage** — the AVNS post-mortem view requires a stable join from archived candidate → the signals that existed pre-announcement. Entity-based match works; semantics need locking. **Decision owner: Pedro (per spec §11).** Recommended default: 90-day window pre-`outcomes.announced_at`, filtered to `signals.entity_id = candidates.entity_id`. Needs confirmation before `/archive/[ticker_mic]` is built in v2.3.
4. **Decisions register surface** — PRD §3 explicitly keeps `DECISIONS.md` as markdown. **Resolved (spec §6c):** ship one markdown file per load-bearing decision at `dashboard/content/decisions/<d_id>.md` with frontmatter (id, title, driver, effective_date, related_docx_sections). No `decisions` table, no schema change, no runtime fetch. Auto-cap chips, convergence fallbacks, and the pre-edge disqualifier link via `/decisions/[d_id]`.
5. **Docx Q-number convention** — current `operator_flags.kind` is free text. Proposal: reserve `kind='known_issue'` + `evidence.q_number` for Q-016 through Q-019 so those four blockers render as stable cards. **Decision owner: Pedro (per spec §11).** Needs confirmation before `/scanners/[name]` auth-blocker chip links to `dashboard/content/known_issues/Q-017.md` in v2.4.

---

## How to review this

Mark each datapoint row with **keep / drop / defer** + optional notes. Rows marked **keep** enter the v2 target surface; rows marked **defer** roll to Phase 5+. Once this doc is locked we invoke `design:design-system` (to define primitives/tokens) and `design:design-critique` (to review each proposed screen against this catalog).
