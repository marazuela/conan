# BC-FDA Light v4 — Phase 2 detail plan: the **synthesis contract** (the moat layer)

> Component owner doc. Scope = the moat sub-component of Phase 2 of `plan-the-high-level-peppy-shell.md`:
> (1) the synthesis **output contract**, (2) the **deterministic threshold + corroboration** logic that
> gates whether the LLM fires, (3) the **Haiku-classify + Sonnet-synthesis** call structure, and
> (4) failure modes / files / tests. The 3 deterministic fetchers themselves (insider, options,
> news) are a *sibling* detail-plan; this doc consumes their outputs (`bc_market_signals`,
> `bc_news_events`) and specifies only the contract it needs from them (§2.1, §9).
>
> **Hard project constraints honored throughout:** zero Cowork; **zero LLM in control flow** — the
> Python threshold (§2) decides whether to call, which events to classify, and whether an escalation is
> permitted; the LLM only *classifies one event* and *writes one synthesis JSON*, and can never widen
> its own gate; **fail-loud** (every run writes a `bc_pipeline_runs` row; invalid synthesis → a logged
> `bc_failed_synthesis_calls` row, never a silent drop); **digest-first** (the contract's top fields are
> exactly what the daily email renders).
>
> Investigation basis (read-only): live schema on `xvwvwbnxdsjpnealarkh` (19 `bc_*` tables, CHECK
> constraints, `bc_config` values), `orchestrator_runtime/client.py` + `pricing.py`, the canonical
> caller pattern in `orchestrator_runtime/runtime.py`, the schema-validation pattern in
> `modal_workers/sub_agents/runtime.py`, the deployed `bc_news_event_upsert` RPC, the existing
> `modal_workers/scanners/insider_form4_scanner.py` + `modal_workers/providers/polygon/`, and
> `~/Downloads/BC_FDA_TOOL_PRODUCT_SPEC.md` §6.4 (L4.1–L4.5). **Migrations are authoritative over the
> spec's printed §7 SQL** (per `supabase_migrations_drift` / `rubric_v2_seed` lessons).

---

## 0. Live-schema facts this plan is pinned to (verified 2026-06-03)

These are the deployed shapes the contract must satisfy *exactly* — do not infer from the spec's §7 text,
which is stale v1.0.

**`bc_thesis_updates`** (synthesis destination):
| column | type | null | note |
|---|---|---|---|
| `id` | uuid | NO | `gen_random_uuid()` |
| `application_number` | text | NO | FK → `bc_applications` |
| `update_date` | date | NO | — |
| `fired_at` | timestamptz | NO | `now()` |
| `trigger_reasons` | **text[]** | **NO** | the deterministic reasons that fired the call (§2.5) |
| `synthesis` | **jsonb** | **NO** | **the contract — §1** |
| `cost_usd` | numeric | YES | Haiku+Sonnet total for the fire |
| `prompt_version` | text | YES | e.g. `bc_synth_v1` |
- **UNIQUE `(application_number, update_date)`** → at most one synthesis row per name per day. Enforced by
  DB *and* re-checked in Python (§2.4 "one fire/day"). Upsert with `ON CONFLICT (application_number,
  update_date) DO NOTHING` (skip-silently semantics; do **not** overwrite an existing fire).

**`bc_failed_synthesis_calls`** (fail-loud sink):
| column | type | null |
|---|---|---|
| `application_number` | text | NO (FK) |
| `attempted_at` | timestamptz | NO `now()` |
| `failure_type` | text | NO — **CHECK ∈ {`schema_violation`,`api_error`,`timeout`,`budget_exceeded`,`implausible`}** |
| `raw_output` | text | YES |
| `error_message` | text | YES |
| `retry_count` | int | YES `0` |

**`bc_news_events`** (Haiku-classify destination, read by synthesis input builder):
- `verdict` text **CHECK ∈ {`confirms_thesis`,`contradicts_thesis`,`neutral_update`,`requires_review`}** (nullable until classified).
- `source_tier` text **CHECK ∈ {`primary`,`secondary`,`low`}**, default `low`.
- `topic` text — **no CHECK** (free text). The plan pins the allowed topic enum in the prompt + a Python
  guard (§3.1); it is not DB-enforced.
- `classifier_confidence` numeric, `classified_at` timestamptz.
- **UNIQUE `(application_number, news_id)`**; `news_id = md5(source|url|published_at)` (set by the RPC).

**`bc_market_signals`** (deterministic streams; read by threshold + provenance):
- `(application_number, signal_date, signal_type, payload jsonb)`, **UNIQUE `(application_number,
  signal_date, signal_type)`**, `computed_at`.

**`bc_rubric_scores`** (score/band, read-only context):
- `risk_band` text **CHECK ∈ {`low`,`moderate`,`elevated`,`high`}**; `p_crl`, `oof_percentile_rank`,
  `ci_low`, `ci_high`, `confidence_flag`, `refusal_reason`. (Per the v4 reframe the synthesis treats
  `risk_band` + `oof_percentile_rank` as the rank input and **must not** echo `p_crl` as a calibrated
  probability — §1.3.)

**`bc_pipeline_runs`** (liveness; **`status` CHECK ∈ {`running`,`succeeded`,`failed`,`partial`}** — verified
live 2026-06-03 via `execute_sql`; writing any other token throws `23514 check_violation`. The run-state
semantics *within* those four tokens are defined in §4.1): `pipeline_name`, `started_at`, `finished_at`,
`status`, `snapshot_date`, `n_processed`, `n_failed`, `cost_usd`, `log jsonb`, `reason`.

**`bc_synthesis_audit`** (weekly grader — out of scope here, but its columns confirm the intended synthesis
shape and constrain the contract): `bullets_up_score 0..3`, `bullets_down_score 0..3`, `risks_score 0..3`,
`recommended_action_correct bool`. ⇒ the contract **must** expose `bullets_up`, `bullets_down`, `risks`
(each ≤3) and a `recommended_action`, or the deployed audit loop cannot grade it.

**`bc_config`** (live values, verified):
- `l4.daily_budget_usd = 5` — **TOTAL** daily spend ceiling (Haiku + Sonnet), hard kill.
- `l4.max_events_per_candidate_day = 40` — per-candidate event cap before deferral.
- The spec's threshold keys (`l4.iv30_dod`, `l4.insider_buy_30d`, …) are **NOT seeded** → this plan adds a
  one-shot seed migration (§2.6). `l3.window_days=120`, `l3.min_market_cap=2.5e8`, `l3.min_adv=2e6` exist
  (universe gates, not ours).

**Reuse the deployed RPC, not raw SQL:** `bc_news_event_upsert(p_application_number, p_source,
p_published_at, p_url, p_raw_text, p_source_tier='low') RETURNS uuid` — SECURITY DEFINER, validates the
application exists + tier, computes `news_id`, `ON CONFLICT … DO NOTHING`. The **fetch** worker writes raw
news via this RPC as the least-priv `bc_scanner` role. Classification (`verdict`/`topic`/`confidence`) is a
**separate UPDATE** path; see §3.4 for the role/grant note (a small RPC `bc_news_event_classify(...)` is
added because `bc_scanner` is capture-only).

---

## 1. THE SYNTHESIS JSON CONTRACT (`bc_thesis_updates.synthesis`)

### 1.0 Design intent

The synthesis is **not** an IC memo. It is the structured payload that (a) the daily email renders verbatim
and (b) the weekly `bc_synthesis_audit` grades. Its single differentiating job: **state the model's
risk view and put it next to what options are already pricing in**, then say — in a constrained enum —
whether that gap is worth a human's attention. Everything is **evidence-grounded**: every claim cites a
deterministic signal id or a classified news id that is present in today's inputs. No free-floating
narrative.

Two corrections vs the spec's §6.4 schema:
1. The high-level plan **cuts** 13F / analyst-targets / price-cohort streams. So the spec's
   `target_price_context.{vs_consensus_pct,dispersion_trend}` are **removed**; the implied-move framing is
   promoted from a buried sub-field to the **top-level core object** `risk_vs_market` (§1.3).
2. The reframe **demotes** `p_crl` to a rank/band. The contract carries `risk_band` + percentile, never a
   displayed probability.

### 1.1 Schema (JSON Schema, Draft-7) — file `schemas/bc_synthesis_v1.json`

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "$id": "bc_synthesis_v1",
  "title": "BC-FDA daily synthesis",
  "type": "object",
  "additionalProperties": false,
  "required": [
    "schema_version", "application_number", "update_date",
    "headline", "what_changed",
    "risk_vs_market",
    "drivers",
    "bullets_up", "bullets_down", "risks",
    "watch_items",
    "recommended_action", "confidence",
    "provenance"
  ],
  "properties": {
    "schema_version":      { "const": "bc_synthesis_v1" },
    "application_number":  { "type": "string", "minLength": 3 },
    "update_date":         { "type": "string", "format": "date" },

    "headline":            { "type": "string", "minLength": 8, "maxLength": 140 },
    "what_changed":        { "type": "string", "minLength": 12, "maxLength": 600 },

    "risk_vs_market": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "model_risk_band", "model_percentile",
        "options_implied_move_pct", "implied_move_horizon",
        "stance", "gap_bps", "rationale"
      ],
      "properties": {
        "model_risk_band":   { "enum": ["low", "moderate", "elevated", "high"] },
        "model_percentile":  { "type": ["number","null"], "minimum": 0, "maximum": 100 },
        "options_implied_move_pct": { "type": ["number","null"], "minimum": 0, "maximum": 300 },
        "implied_move_horizon":     { "enum": ["pdufa", "next_earnings", "30d", "unavailable"] },
        "stance": {
          "enum": [
            "market_underpricing_risk",
            "market_overpricing_risk",
            "aligned",
            "indeterminate_no_options"
          ]
        },
        "gap_bps":   { "type": ["number","null"] },
        "rationale": { "type": "string", "minLength": 10, "maxLength": 400 }
      }
    },

    "drivers": {
      "type": "array", "minItems": 1, "maxItems": 4,
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["stream", "direction", "magnitude", "evidence_ref", "summary"],
        "properties": {
          "stream":    { "enum": ["insider", "options", "news"] },
          "direction": { "enum": ["bullish", "bearish", "neutral"] },
          "magnitude": { "enum": ["minor", "notable", "major"] },
          "evidence_ref": {
            "type": "object",
            "additionalProperties": false,
            "required": ["kind", "id"],
            "properties": {
              "kind": { "enum": ["market_signal", "news_event"] },
              "id":   { "type": "string", "minLength": 8 },
              "metric": { "type": ["string","null"] },
              "value":  { "type": ["number","string","null"] }
            }
          },
          "summary": { "type": "string", "minLength": 6, "maxLength": 200 }
        }
      }
    },

    "bullets_up":   { "type": "array", "maxItems": 3, "items": { "type": "string", "minLength": 4, "maxLength": 180 } },
    "bullets_down": { "type": "array", "maxItems": 3, "items": { "type": "string", "minLength": 4, "maxLength": 180 } },
    "risks":        { "type": "array", "maxItems": 3, "items": { "type": "string", "minLength": 4, "maxLength": 180 } },

    "watch_items": {
      "type": "array", "minItems": 1, "maxItems": 2,
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["label", "why"],
        "properties": {
          "label": { "type": "string", "minLength": 3, "maxLength": 120 },
          "why":   { "type": "string", "minLength": 6, "maxLength": 200 },
          "evidence_ref": {
            "type": ["object","null"],
            "additionalProperties": false,
            "required": ["kind", "id"],
            "properties": {
              "kind": { "enum": ["market_signal", "news_event"] },
              "id":   { "type": "string" }
            }
          }
        }
      }
    },

    "recommended_action": { "enum": ["no_change", "monitor", "investigate", "exit"] },
    "confidence":         { "type": "number", "minimum": 0, "maximum": 1 },

    "provenance": {
      "type": "object",
      "additionalProperties": false,
      "required": ["market_signal_ids", "news_event_ids", "score_id", "input_window_days", "streams_available"],
      "properties": {
        "market_signal_ids": { "type": "array", "items": { "type": "string" } },
        "news_event_ids":    { "type": "array", "items": { "type": "string" } },
        "score_id":          { "type": ["string","null"] },
        "input_window_days": { "type": "integer", "minimum": 1, "maximum": 90 },
        "streams_available": {
          "type": "object",
          "additionalProperties": false,
          "required": ["insider", "options", "news"],
          "properties": {
            "insider": { "type": "boolean" },
            "options": { "type": "boolean" },
            "news":    { "type": "boolean" }
          }
        }
      }
    }
  }
}
```

### 1.2 Field-by-field semantics

| field | meaning / constraint |
|---|---|
| `schema_version` | pinned const; also written to `bc_thesis_updates.prompt_version`. Bump when the contract changes. |
| `headline` | one-line digest subject for this name. The email's per-name row title. |
| `what_changed` | 1–3 sentences: the *deterministic* change(s) that crossed threshold today. Must reference at least one driver. |
| `risk_vs_market` | **the moat object — see §1.3.** |
| `drivers` | 1–4 items, **one per stream that moved**, each pinned to a real `evidence_ref.id` that exists in today's `provenance`. `direction`/`magnitude` are the model's read of the deterministic evidence, not new facts. |
| `bullets_up/down/risks` | ≤3 each (the audit grades 0..3). Each bullet must be supportable by a `drivers` item or the score context; no invented facts. Empty arrays are valid (e.g. nothing bearish). |
| `watch_items` | **the "1–2 names/items worth a look"** — the digest's call-to-attention. 1–2 only. |
| `recommended_action` | **constrained enum** `no_change \| monitor \| investigate \| exit`. **Escalation past `monitor` is gated in Python (§2.3) — the model may *propose* it but the corroboration rule can downgrade it before persist.** |
| `confidence` | model's self-rated 0..1. Used by the digest alert gate (`investigate`/`exit` + `confidence ≥ 0.6`, per spec §6.6) — but only *after* the Python corroboration gate already permitted the escalation. |
| `provenance` | **explicit citation of every deterministic signal** the synthesis is allowed to reference. `market_signal_ids`/`news_event_ids` are the universe of ids the model was handed; every `evidence_ref.id` in `drivers`/`watch_items` **must be a member** (validated in §3.5). `streams_available` records which of the 3 streams produced data today (lets the digest say "options data unavailable" honestly). |

### 1.3 `risk_vs_market` — the differentiating object

This is the product. It states the **model's risk view** and the **options-implied move**, then classifies
the relationship in a fixed `stance` enum so the digest and the audit can both reason about it
deterministically:

- `model_risk_band` / `model_percentile` — copied (not re-derived) from the latest `bc_rubric_scores` row
  for this application. **Never a probability.** If no score row exists, band = the row's band or `null`
  percentile, and `stance` is forced to `indeterminate_no_options` only if options are also missing;
  otherwise the model still compares qualitatively.
- `options_implied_move_pct` — the straddle-implied move from today's `options` market-signal payload
  (`signal_type='options_iv'`, payload key `implied_move_pct_pdufa` preferred, else `implied_move_pct_30d`).
  `null` if the options stream was unavailable.
- `implied_move_horizon` — which horizon the implied move is quoted at; `unavailable` when null.
- `stance` — **the core read**:
  - `market_underpricing_risk` — model band is `elevated`/`high` (or percentile high) but the implied move
    is *small* relative to a CRL-grade binary. (The interesting asymmetry: market complacent, model worried.)
  - `market_overpricing_risk` — model band is `low`/`moderate` but the implied move is *large*. (Market
    bracing harder than the model.)
  - `aligned` — band and implied move agree directionally.
  - `indeterminate_no_options` — options stream missing today; emit band only, no comparison. **Forces
    `options_implied_move_pct=null`, `implied_move_horizon='unavailable'`, and caps the day's escalation at
    `monitor`** (a missing core stream cannot drive `investigate`/`exit` — §2.3).
- `gap_bps` — optional numeric gap (model-implied vs market-implied), null when not computable.
- `rationale` — 1–2 sentences justifying the `stance`, citing the band and the implied-move number.

The **Python side computes a deterministic `stance_hint`** (a coarse band×implied-move lookup, §2.4) and
passes it into the prompt as context; the model must either agree or justify a different `stance` in
`rationale`. A mismatch between `stance` and `stance_hint` where the model escalates is a **plausibility
failure** (§3.5) unless corroborated.

### 1.4 Worked example (schema-valid)

Scenario: PRTX BLA-761333, PDUFA in 41 days, model `risk_band='elevated'`, percentile 78th. Today's
deterministic deltas: an insider cluster (2 directors + CFO open-market buys, net +$2.1M / 14d), IV30 +7pp
DoD with a front-month spike, and a PR-wire 8-K classified `confirms_thesis`/`manufacturing_buildout`
(tier `low`). Options implied move ±14% at PDUFA — **small** for an elevated-risk binary ⇒ market may be
underpricing.

```json
{
  "schema_version": "bc_synthesis_v1",
  "application_number": "BLA-761333",
  "update_date": "2026-06-03",
  "headline": "PRTX: insider cluster + IV pop, but options still pricing a calm ±14% into an elevated-risk PDUFA",
  "what_changed": "Three insiders (2 directors, CFO) bought $2.1M open-market over 14 days and IV30 rose 7pp day-over-day with a front-month spike, 41 days before PDUFA. A manufacturing-buildout 8-K (PR-wire, unverified) also hit.",
  "risk_vs_market": {
    "model_risk_band": "elevated",
    "model_percentile": 78,
    "options_implied_move_pct": 14.0,
    "implied_move_horizon": "pdufa",
    "stance": "market_underpricing_risk",
    "gap_bps": null,
    "rationale": "Model sits in the elevated band (78th pct of cohort) yet the straddle prices only a ±14% PDUFA move, light for a first-cycle BLA with CRL-grade downside; the insider buying and IV pop are early tells the option market has not fully absorbed."
  },
  "drivers": [
    {
      "stream": "insider", "direction": "bullish", "magnitude": "notable",
      "evidence_ref": { "kind": "market_signal", "id": "f3a1c9d2-...-insider_cluster_buy", "metric": "net_buy_usd_14d", "value": 2100000 },
      "summary": "Cluster: 2 directors + CFO open-market buys, $2.1M / 14d, no 10b5-1."
    },
    {
      "stream": "options", "direction": "neutral", "magnitude": "notable",
      "evidence_ref": { "kind": "market_signal", "id": "9b22e7f0-...-options_iv", "metric": "iv30_dod_pp", "value": 7.0 },
      "summary": "IV30 +7pp DoD, front-month-loaded; implied ±14% at PDUFA still modest."
    },
    {
      "stream": "news", "direction": "bullish", "magnitude": "minor",
      "evidence_ref": { "kind": "news_event", "id": "c71d...-md5", "metric": "topic", "value": "manufacturing_buildout" },
      "summary": "8-K signals manufacturing scale-up (PR-wire, low tier — corroborating only)."
    }
  ],
  "bullets_up": [
    "Insider cluster (C-suite + directors) buying into the PDUFA window",
    "Manufacturing-buildout disclosure consistent with launch confidence"
  ],
  "bullets_down": [
    "Options market pricing a calm ±14% — limited convexity if approval surprises"
  ],
  "risks": [
    "Elevated model band (78th pct) — first-cycle CRL base rate is material",
    "Manufacturing 8-K is PR-wire/unverified; not independently corroborated"
  ],
  "watch_items": [
    {
      "label": "IV term-structure inversion before PDUFA",
      "why": "Front-month spike without back-month follow-through often precedes a re-rate; track for 5 sessions.",
      "evidence_ref": { "kind": "market_signal", "id": "9b22e7f0-...-options_iv" }
    }
  ],
  "recommended_action": "monitor",
  "confidence": 0.66,
  "provenance": {
    "market_signal_ids": ["f3a1c9d2-...-insider_cluster_buy", "9b22e7f0-...-options_iv"],
    "news_event_ids": ["c71d...-md5"],
    "score_id": "5d0e...-rubric",
    "input_window_days": 7,
    "streams_available": { "insider": true, "options": true, "news": true }
  }
}
```

Note the example **does not** escalate to `investigate` despite the suggestive setup: the only escalation
candidates were a single low-tier news verdict (cannot escalate alone, §2.3) and an insider co-signal that
is *bullish* (a buy cluster does not justify `exit`, and `investigate` here would require the corroboration
rule to be satisfied for a downside thesis). Python therefore caps the action at `monitor` — demonstrating
the control flow gates the model, not vice-versa.

---

## 2. THE DETERMINISTIC THRESHOLD + CORROBORATION LOGIC (pure Python — the gate)

**Module:** `modal_workers/bc_monitor/threshold.py`. **No Anthropic import in this module** — it decides
firing from `bc_market_signals` + classified `bc_news_events` rows only. This is the control-flow boundary:
the LLM is never consulted about *whether* to fire or *whether* to escalate.

### 2.1 Inputs the threshold reads (contract it requires from the sibling fetchers)

Per application, for `signal_date = today`:
- **Insider** market-signal rows (`signal_type ∈ {insider_cluster_buy, insider_cluster_sell,
  c_suite_open_market_buy}` — these already exist verbatim in `insider_form4_scanner.py`). Payload keys the
  threshold consumes: `net_buy_usd_30d`, `net_sell_usd_30d`, `n_insiders`, `cluster` (bool),
  `direction`, `roles[]`, `has_10b5_1_only` (bool).
- **Options** market-signal row (`signal_type='options_iv'`). Payload keys: `iv30`, `iv30_dod_pp`,
  `iv60`, `iv90`, `front_back_slope`, `slope_inverted` (bool), `unusual_volume` (bool),
  `implied_move_pct_pdufa`, `implied_move_pct_30d`, `implied_move_horizon`. **May be absent** (Polygon
  options tier not yet confirmed — see §8 risk); absence sets `streams_available.options=false`.
- **News** rows from `bc_news_events` for this application, `published_at` within the **7d** input window,
  already classified (`verdict` not null). Fields: `verdict`, `topic`, `source_tier`,
  `classifier_confidence`, `news_id`, `id`.
- **Score context:** latest `bc_rubric_scores` row (`risk_band`, `oof_percentile_rank`, `id`). Read-only.

### 2.2 Per-stream trigger predicates (thresholds read from `bc_config`, §2.6)

A stream "fires" (adds a `trigger_reason`) when **any** predicate is true. Each reason is a stable token
written to `bc_thesis_updates.trigger_reasons` and used in provenance.

**Insider** (`bc_config: l4.insider_buy_30d`, `l4.insider_sell_30d`):
- `insider_buy_cluster` — `cluster AND direction='buy' AND net_buy_usd_30d > l4.insider_buy_30d`.
- `insider_sell_cluster` — `cluster AND direction='sell' AND net_sell_usd_30d > l4.insider_sell_30d AND n_insiders >= 3`.
- `csuite_open_market_buy` — presence of a `c_suite_open_market_buy` signal today.
- (10b5-1-only rows are already dropped upstream; the threshold additionally ignores any row with
  `has_10b5_1_only=true` as a guard.)

**Options** (`bc_config: l4.iv30_dod`, default 6pp):
- `iv30_jump` — `abs(iv30_dod_pp) > l4.iv30_dod`.
- `iv_slope_inversion` — `slope_inverted = true`.
- `unusual_options_volume` — `unusual_volume = true`.
- `implied_move_shift` — `abs(implied_move_pct_today − implied_move_pct_prev) > l4.implied_move_shift_pp`
  (new key, default 4pp), computed from yesterday's `options_iv` payload when present.

**News** (`bc_config: l4.news_positive_run`, default 3):
- `news_contradicts` — any row `verdict='contradicts_thesis'` in window. *(flag for synthesis; escalation
  still gated — §2.3)*.
- `news_requires_review` — any row `verdict='requires_review'`.
- `news_positive_run` — `count(verdict='confirms_thesis' in 7d) >= l4.news_positive_run`.
- `commercial_plan_signal` — any row `topic ∈ {commercial_launch_prep, manufacturing_buildout,
  sales_force_hiring, distribution_partnership, payer_or_pricing_signal}`.
- `commercial_plan_cluster` — `>= l4.commercial_cluster_30d` (default 2) such topic hits in 30d → adds a
  distinct stronger reason.

If `trigger_reasons` is empty after evaluating all three streams ⇒ **no LLM call; cost for the day = $0**;
the monitor still writes its `bc_pipeline_runs` row and a per-name "evaluated, no_fire" log entry.

### 2.3 CORROBORATION RULE — the escalation gate (deterministic, pre-LLM and post-LLM)

The model proposes a `recommended_action`; **Python decides the maximum action it is allowed to persist.**
Two enforcement points:

**(A) Pre-LLM — compute `max_action_allowed`** from the deterministic reasons, and pass it into the prompt
as a hard ceiling:

```
escalation_evidence = set()
# A deterministic co-signal counts as corroboration:
if any insider/options reason present:        escalation_evidence.add("deterministic_cosignal")
# >=2 INDEPENDENT news sources (distinct source domains) agreeing in direction:
if n_independent_sources(direction) >= 2:      escalation_evidence.add("multi_source")
# A PRIMARY-tier source (EDGAR 8-K / FDA) on its own can corroborate:
if any news row with source_tier='primary':    escalation_evidence.add("primary_source")

has_negative_signal = (
    'news_contradicts' in reasons or 'news_requires_review' in reasons
    or 'insider_sell_cluster' in reasons
    or 'iv_slope_inversion' in reasons          # downside microstructure tell
)

if has_negative_signal and escalation_evidence:
    max_action_allowed = 'investigate'          # 'exit' is never auto-permitted in v1 (see note)
elif escalation_evidence:
    max_action_allowed = 'monitor'              # positive-only corroboration → monitor ceiling
else:
    max_action_allowed = 'monitor'              # default ceiling; single low-tier verdict cannot exceed
# Hard caps that override the above:
if streams_available.options is False and stance needs options:
    max_action_allowed = min(max_action_allowed, 'monitor')   # missing core stream
if ONLY a single low-tier news verdict triggered (no deterministic co-signal, no 2nd source):
    max_action_allowed = 'monitor'              # spec §6.4: low source_tier cannot escalate alone
```

Rules encoded:
- **No single LLM verdict alone escalates to `investigate`/`exit`.** A lone `contradicts_thesis` from a
  `low`-tier PR-wire yields `max_action_allowed='monitor'`.
- **Escalation requires a corroborating deterministic co-signal OR ≥2 independent sources OR a primary-tier
  source.**
- **`exit` is not auto-permitted in v1** (the monitor is advisory; `exit` would imply a position the tool
  does not track). `exit` is reserved for an operator override path (`bc_operator_overrides`). The enum
  keeps `exit` so the contract is forward-compatible and the audit can grade an operator-set `exit`, but
  the Python ceiling caps model-authored actions at `investigate`. *(If Pedro wants the model to author
  `exit`, raise the ceiling here — single-line change — but keep the corroboration requirement.)*

**(B) Post-LLM — clamp:** after parsing, `final_action = min_by_severity(model.recommended_action,
max_action_allowed)` where severity order is `no_change < monitor < investigate < exit`. If the model
returned a *higher* action than allowed, the action is **downgraded** (not rejected), the original is kept
in `synthesis.recommended_action` *after* clamping, and a `provenance`-adjacent note
`action_clamped_from=<model_value>` is recorded in the `bc_pipeline_runs.log` for that name (not in the
contract, to keep the rendered payload clean). A model action that *exceeds* allowance **and** disagrees
with `stance_hint` with no corroboration is additionally a plausibility failure (§3.5) — but the clamp
already prevents an over-escalation from ever reaching the digest, so the failure path is for telemetry, not
gating.

> **Net invariant:** the worst a hallucinating model can do is get *downgraded to `monitor`* and logged. It
> can never push an `investigate`/`exit` to the operator without a deterministic corroborator. This is the
> "LLM never gates itself" guarantee in code.

### 2.4 Deterministic `stance_hint` (band × implied-move lookup)

Coarse table the Python passes to the prompt (and uses to detect implausible escalations):

| model band | implied move | stance_hint |
|---|---|---|
| elevated/high | < 20% | `market_underpricing_risk` |
| elevated/high | ≥ 20% | `aligned` |
| low/moderate | ≥ 30% | `market_overpricing_risk` |
| low/moderate | < 30% | `aligned` |
| any | options unavailable | `indeterminate_no_options` |

(The 20%/30% cutpoints are `bc_config: l4.implied_move_low_band_pct` / `l4.implied_move_high_band_pct`,
defaults 20 / 30, tunable. They are deliberately wide; the model's `rationale` carries the nuance.)

### 2.5 Per-name decision flow (one function, returns a typed decision)

```
decide(app) -> Decision(should_fire: bool,
                        trigger_reasons: list[str],
                        max_action_allowed: str,
                        stance_hint: str,
                        input_bundle: SynthesisInputs)
```
1. Load today's insider/options market-signals, 7d classified news, latest score.
2. Evaluate §2.2 predicates → `trigger_reasons`.
3. If empty → `should_fire=False` (record "no_fire"); return.
4. Check the DB `UNIQUE(application_number, update_date)` guard — if a row already exists for today,
   `should_fire=False`, reason `already_fired_today` (idempotent re-runs are safe).
5. Compute `max_action_allowed` (§2.3) + `stance_hint` (§2.4).
6. Assemble `SynthesisInputs` (the exact bundle handed to Sonnet — §3.3) including the provenance id lists.
7. Return `should_fire=True`.

### 2.6 New `bc_config` seeds (one-shot migration, MCP-applied per drift discipline)

Add to `bc_config` (jsonb scalar values, matching the live `l4.` prefix convention; **write the disk
migration first**, then apply — see `feedback_mcp_apply_migration_discipline`). All tunable without redeploy:

| key | default | purpose |
|---|---|---|
| `l4.insider_buy_30d` | `1000000` | net insider buy $ over 30d to fire |
| `l4.insider_sell_30d` | `2000000` | net insider sell $ over 30d to fire |
| `l4.iv30_dod` | `6` | IV30 day-over-day pp change to fire |
| `l4.implied_move_shift_pp` | `4` | implied-move pp shift DoD to fire |
| `l4.news_positive_run` | `3` | confirms_thesis count in 7d to fire |
| `l4.commercial_cluster_30d` | `2` | commercial-topic hits in 30d to escalate-flag |
| `l4.implied_move_low_band_pct` | `20` | stance_hint cutpoint (elevated/high band) |
| `l4.implied_move_high_band_pct` | `30` | stance_hint cutpoint (low/moderate band) |
| `l4.synthesis_dry_run` | `false` | when true: run threshold + build inputs, **skip the LLM**, log would-fire to `bc_pipeline_runs.log` (the 7-day warm-up per spec §A3 / `dry_run` step). |
| `l4.near_dup_window_days` | `2` | dedup window for near-duplicate news before classify (§3.2). |

`bc_config` reads go through a tiny cached helper `bc_monitor/config.py:get_float(key, default)` /
`get_bool(key, default)` (reads the jsonb scalar via `value #>> '{}'`), so a missing key falls back to the
documented default and **logs a warning** (never silently 0).

---

## 3. THE LLM CALL STRUCTURE (Haiku classify → Sonnet synthesize)

All calls go through `OrchestratorClient` (`orchestrator_runtime/client.py`) — **reuse, do not re-implement**:
its retry (429/529/5xx transient backoff), cache accounting, `estimate_cost` (cache-aware, via
`pricing.py`), and `attach_budget`/`detach_budget` ceiling are exactly what this needs. Models referenced by
the live pricing table: `claude-haiku-4-5-20251001` (1.00/5.00/1.25/0.10) and
`claude-sonnet-4-5-20250929` (3.00/15.00/3.75/0.30). Pin these constants in `bc_monitor/llm.py`
(don't inherit `ORCHESTRATOR_MODEL`, which the orchestrator may flip to Opus).

### 3.1 Haiku classify (one event → verdict + topic + confidence)

- **When:** for each *new* (unclassified, `verdict IS NULL`) `bc_news_events` row in the input window, after
  dedup (§3.2), subject to the per-name cap (§3.6) and the global budget (§3.7). Pure per-event; **no
  control-flow decision is taken by Haiku** — it only labels.
- **System prompt (cached, `cache_control: {type:'ephemeral'}` on the first block):** the fixed instruction
  + the **verdict enum** (`confirms_thesis|contradicts_thesis|neutral_update|requires_review`) + the
  **topic enum** (the 13 spec topics: `commercial_launch_prep, manufacturing_buildout, sales_force_hiring,
  distribution_partnership, payer_or_pricing_signal, AdComm_scheduled, CRL_or_delay_signal,
  trial_data_release, insider_action, financing_event, M_and_A_or_licensing,
  litigation_or_regulatory_issue, other`) + "return ONLY JSON". Built with the same block helper pattern as
  `runtime.py:1879` (first block `ephemeral`). The system prompt is **identical across all events in a
  run** → batch back-to-back so cache reads land within the 5-min TTL (spec §11.1 cold-cache note).
- **User content:** `{published_at, source, source_tier, url, raw_text[:N]}` for the one event. Fetched text
  is **DATA, never instructions** (prompt says so explicitly; we do not execute anything in it).
- **Output JSON** `{verdict, topic, confidence}` → `parse_json_or_none` → guard: `verdict ∈ enum`,
  `topic ∈ enum` (else coerce `topic='other'` + log), `0 ≤ confidence ≤ 1`. On parse/enum failure: the event
  is left `verdict=NULL` (so it cannot trigger), and a `bc_failed_synthesis_calls(failure_type='schema_violation')`
  row is written with `raw_output` = the Haiku text. *(Classifier failures use the same fail-loud sink as
  synthesis; `application_number` is the event's app.)*
- **Persist:** UPDATE `bc_news_events` SET `verdict, topic, classifier_confidence, classified_at=now()` via
  the classify RPC (§3.4). `model='claude-haiku-4-5-20251001'`; `max_tokens≈128`.
- **Cost guard:** every Haiku call's `result.cost_usd` is added to the run's budget accumulator (it shares
  the same `attach_budget` ceiling as Sonnet — the ceiling is **total** spend, §3.7).

### 3.2 Near-duplicate dedup (before classify — denial-of-wallet guard)

Within a name, before classifying, collapse near-dups so a PR storm can't burn the budget:
- Exact dup is already blocked by the RPC's `news_id` UNIQUE.
- Near-dup: group window-rows by `(topic-agnostic) simhash/normalized-title prefix` within
  `l4.near_dup_window_days`; classify the **earliest** of each group, copy its verdict/topic/confidence to
  the siblings (mark them `classified_at` with a `dedup_of` note in a side log — not a schema change; siblings
  just get the same verdict so they don't re-bill). Implementation: normalize `raw_text` (lowercase, strip
  whitespace/urls), hash the first 200 chars; identical hash within the window ⇒ same group. Cheap, stdlib.

### 3.3 Sonnet synthesize (the contract — §1)

- **When:** exactly once per name per day, only if `decide().should_fire` (§2.5) and not dry-run and budget
  not exceeded and no existing row for today.
- **Input bundle (`SynthesisInputs`)** — handed to the model, all deterministic:
  - **Today's deltas:** the firing insider + options market-signal payloads (id + metric values).
  - **Non-neutral news (7d):** the classified rows with `verdict ≠ neutral_update` (id, source, source_tier,
    verdict, topic, confidence, short snippet).
  - **Score/band context (read-only):** `risk_band`, `oof_percentile_rank`, `confidence_flag`, `score_id`,
    `pdufa_date`, `days_to_pdufa`.
  - **Options-implied move:** `implied_move_pct_*`, `implied_move_horizon` (or "unavailable").
  - **Control context:** `trigger_reasons`, `stance_hint`, **`max_action_allowed`** (the ceiling), the
    explicit `provenance` id lists the model must cite from, and `streams_available`.
- **System prompt (cached):** the role ("BC-FDA daily monitor synthesizer"), the **full Draft-7 schema
  inlined** (same injection style as `sub_agents/runtime.py:273` "Your final answer MUST validate against
  this exact Draft-7 schema… do not invent top-level keys"), the **rules**:
  1. Output ONLY the JSON object, no prose.
  2. `risk_band`/`percentile` are copied from context — never invent a probability, never display `p_crl`.
  3. Every `evidence_ref.id` MUST be a member of the provided `provenance` id lists.
  4. `recommended_action` MUST be ≤ `max_action_allowed` (you may go lower; never higher).
  5. `stance` must match `stance_hint` unless you justify a deviation in `rationale`.
  6. Bullets/risks must be grounded in `drivers` or score context; ≤3 each.
  - The system block is identical across all names in a run → the second cached block (per-name inputs) is
    the only thing that changes; batch back-to-back for cache reuse.
- **Call:** `a_client.call(system=system_blocks, messages=[{role:'user', content:<bundle JSON>}],
  model='claude-sonnet-4-5-20250929', max_tokens≈1200)`. No thinking (Sonnet skips the interleaved-thinking
  header per `client.py:186`). `temperature` omitted (Sonnet rejects it).
- **Parse + validate + clamp + persist:** §3.5.

### 3.4 RPC / role note (capture-only `bc_scanner` can't UPDATE classifications)

The deployed `bc_news_event_upsert` is **insert-only** and `bc_scanner` is least-priv (migration 003). The
classifier must UPDATE `verdict/topic/confidence`. Two clean options — **plan picks (a):**
- **(a)** add a small SECURITY DEFINER RPC `bc_news_event_classify(p_id uuid, p_verdict text, p_topic text,
  p_confidence numeric)` that validates the verdict enum + range and UPDATEs the row; grant EXECUTE to
  `bc_scanner`. Mirrors the capture RPC's trust-boundary discipline (fetched text/labels never hit raw
  `execute_sql`/service-role). Ship its SQL in the same migration as the §2.6 config seeds.
- (b) run the classify/synthesis worker under service-role directly. Rejected: violates the §14 trust
  boundary the spec explicitly closed; keep the metered worker least-priv.
- The synthesis **persist** to `bc_thesis_updates` likewise gets a `bc_thesis_update_upsert(p_app, p_date,
  p_trigger_reasons text[], p_synthesis jsonb, p_cost numeric, p_prompt_version text)` RPC with
  `ON CONFLICT (application_number, update_date) DO NOTHING`, grant to `bc_scanner`. Failures persist via
  `bc_failed_synthesis_upsert(...)`. (All three RPCs in one migration; re-introspect grants after apply.)

### 3.5 Validation + plausibility (fail-loud; **no silent skip**)

Order of operations after the Sonnet call returns:
1. `parse_json_or_none(result.text)`. `None` ⇒ `bc_failed_synthesis_calls(failure_type='schema_violation',
   raw_output=result.text, error_message='unparseable')`; **no thesis row**.
2. **Schema validation** against `schemas/bc_synthesis_v1.json` using `jsonschema.Draft7Validator`
   (same `_validate` helper shape as `sub_agents/runtime.py:148`). **Override the "skipped if not installed"
   fallback:** in this worker, `jsonschema` is a hard dependency — if the import fails, **raise** (the moat
   layer must never persist an unvalidated payload). Any validator error ⇒
   `failure_type='schema_violation'`, error_message = first error string; **no thesis row**.
3. **Plausibility checks** (cross-field, deterministic — `failure_type='implausible'` on any breach):
   - every `drivers[*].evidence_ref.id` and `watch_items[*].evidence_ref.id` ∈ the union of
     `provenance.market_signal_ids ∪ news_event_ids` (no fabricated citations).
   - `risk_vs_market.model_risk_band` == the score row's band (model didn't alter it); `model_percentile`
     within ±1 of the score row (rounding tolerance).
   - if `streams_available.options=false` then `options_implied_move_pct=null` AND
     `implied_move_horizon='unavailable'` AND `stance='indeterminate_no_options'`.
   - `recommended_action ≤ max_action_allowed` **after clamping** (clamp first, then assert; a model value
     above ceiling is downgraded in §2.3-B and logged, not failed — but if the *clamped* value is still
     inconsistent, fail).
   - each non-empty `bullets_*`/`risks` entry references a stream/topic present in inputs (lightweight: at
     least one `drivers.summary` keyword or score term appears; this is a *soft* check that logs a warning
     rather than failing, to avoid false rejects — only the citation/band/options checks hard-fail).
4. **Persist (valid + plausible):** clamp action (§2.3-B), set `synthesis.recommended_action=final_action`,
   write `bc_thesis_updates` via the upsert RPC with `trigger_reasons`, `cost_usd` (Haiku+Sonnet sum for
   this name), `prompt_version='bc_synthesis_v1'`. `update_date=today`.
5. Every failure path writes the `bc_failed_synthesis_calls` row **and** increments `n_failed` on the run.

### 3.6 Per-name cap

- Classify at most `l4.max_events_per_candidate_day` (live = 40) events per name per day; overflow events are
  **deferred** (left unclassified, logged with reason `event_cap_deferred`) and a
  `operator_flags(source='bc_event_cap', severity='warn')` is raised (migration 005 must be applied first —
  see §4.3). Dedup (§3.2) runs *before* the cap so dups don't consume the budget.
- At most **one** Sonnet synthesis per name per day (DB UNIQUE + the §2.5 step-4 guard).

### 3.7 Budget ceiling (total Haiku + Sonnet kill)

- One `attach_budget(run_id=<pipeline_run_id>, hard_kill_usd=get_float('l4.daily_budget_usd', 5.0))` at the
  **start of the whole daily monitor run** (not per name), wrapped in try/finally with `detach_budget()` —
  same pattern as `runtime.py:2038`. The accumulator spans **all** Haiku + Sonnet calls across all names
  (the ceiling is total spend, per spec §11.4).
- On `BudgetExceededError`: stop issuing *any* further metered calls; mark the run **`status='failed'` with
  `reason='killed_budget'` and `log.kill='budget'`** (the CHECK forbids a dedicated `killed_budget` token —
  the budget-kill nuance is carried in `reason`/`log`, §4.1), write the partial `cost_usd` (from
  `get_accumulated_cost()` before detach), raise `operator_flags(source='bc_daily_budget',
  severity='critical')`, and write a `bc_failed_synthesis_calls(failure_type='budget_exceeded')` row for the
  name that tripped it. Remaining names are recorded as `deferred_budget` in `bc_pipeline_runs.log`.
  **Fail-loud, not silent.**
- Because the budget raises *after* a call is already paid for, the partial cost is always captured (matches
  `BudgetExceededError` semantics in `client.py:285`).

### 3.8 Cost sanity (from live pricing table)

Per spec §11 with the v4-trimmed stream set: Haiku ≈ $0.0008–0.00125/event, Sonnet ≈ $0.019–0.0255/fire.
For ~20 names, worst case ~all-fire day ≈ 20 × (≈10 events × $0.001 + 1 × $0.025) ≈ **$0.70/day** — well
under the $5 ceiling, which therefore behaves as a runaway guard, not a normal-day constraint.

---

## 4. FAILURE MODES + FAIL-LOUD + LIVENESS

### 4.1 `bc_pipeline_runs` contract (liveness = "did today's run write its row?")

`pipeline_name='bc_daily_monitor'`. One row per daily run. `status` is **CHECK-constrained ∈
{`running`,`succeeded`,`failed`,`partial`}** (verified live 2026-06-03 — writing any other token throws
`23514 check_violation`): `running` → terminal one of `succeeded` | `partial` | `failed`. The two failure
*causes* the monitor distinguishes (budget-kill vs uncaught crash) are encoded in `reason` + `log`, **not**
a dedicated status token:
- `succeeded` — all names evaluated, all fires persisted, `n_failed=0`.
- `partial` — some names failed (schema/implausible/api) but the run completed; `n_failed>0`.
- `failed` — the run did not complete cleanly. Two sub-causes, disambiguated in `reason`/`log` (not status):
  - **budget kill** (§3.7): `reason='killed_budget'`, `log.kill='budget'`, partial `cost_usd` recorded.
  - **uncaught throw**: `reason` = exception summary. **The cron must write this row even on crash** (outer
    try/finally), else liveness goes blind — the explicit anti-pattern from
    `dispatch_observability_silent_swallow` / `cowork_session_halt`.
- `log` jsonb additionally carries per-name outcomes (`fired`/`no_fire`/`failed:<type>`/`deferred`) and
  `action_clamped_from` notes.

### 4.2 Failure-mode table

| failure | detection | behavior | sink |
|---|---|---|---|
| Sonnet unparseable JSON | `parse_json_or_none` None | no thesis row; continue other names | `bc_failed_synthesis_calls(schema_violation)` |
| Sonnet schema-invalid | `Draft7Validator` errors | no thesis row | `…(schema_violation)` + first error msg |
| Sonnet implausible (citation/band/options/clamp) | §3.5.3 | no thesis row | `…(implausible)` |
| Haiku unparseable/enum-bad | parse/enum guard | event stays `verdict=NULL` (can't trigger) | `…(schema_violation)` |
| Anthropic API error (post-retry) | `client.call` raises after backoff | name marked failed; continue | `…(api_error)` |
| budget exceeded | `BudgetExceededError` | stop all metered calls; run `failed` (`reason='killed_budget'`, `log.kill='budget'`) | `…(budget_exceeded)` + critical flag |
| options stream absent | `streams_available.options=false` | synthesis still runs (band-only), action capped at `monitor`; **not** a failure | `bc_pipeline_runs.log` note |
| `jsonschema` not importable | hard ImportError | **raise** — refuse to run unvalidated | run `status='failed'` (`reason`=ImportError) |
| dup re-run same day | DB UNIQUE / step-4 guard | skip silently, idempotent | log `already_fired_today` |
| missing `bc_config` key | `config.get_*` fallback | use documented default | warn log |
| migration 005 not applied | INSERT to `operator_flags` rejects bc_ source | **pre-flight check at worker start**; if absent, downgrade flag writes to `bc_pipeline_runs.log` and emit a single startup warning (don't crash the monitor for a flag-sink gap) | startup log |

### 4.3 Migration 005 dependency

`operator_flags` bc_ sources (`bc_event_cap`, `bc_daily_budget`, `bc_synthesis_failed`, …) require
**migration 005** (`005_operator_flags_bc_sources.sql`, currently **not applied** — high-level plan
cross-cutting note). The worker pre-flights the live `operator_flags` source CHECK (re-introspect, per
`migration_drift_sweep` discipline) and, if bc_ sources aren't allowed yet, routes "flag" intents to
`bc_pipeline_runs.log` instead of crashing. Applying 005 is a cross-cutting prerequisite, not this
component's deliverable.

---

## 5. FILES TO CREATE / TOUCH (paths)

```
modal_workers/bc_monitor/__init__.py
modal_workers/bc_monitor/config.py          # bc_config cached reader (get_float/get_bool, warn-on-default)
modal_workers/bc_monitor/threshold.py       # §2 — pure-Python gate, no anthropic import
modal_workers/bc_monitor/llm.py             # §3 — Haiku classify + Sonnet synthesize via OrchestratorClient
modal_workers/bc_monitor/contract.py        # load schema, validate (hard jsonschema), plausibility, clamp
modal_workers/bc_monitor/persist.py         # RPC calls: classify / thesis upsert / failed upsert / pipeline_run
modal_workers/bc_monitor/run_daily.py       # orchestration: per-name decide()->classify->synthesize; budget; bc_pipeline_runs row
schemas/bc_synthesis_v1.json                # §1.1 — the contract (Draft-7); load via importlib.resources/Path
prompts/bc_synth_system.md                  # cached Sonnet system prompt body (schema injected at build time)
prompts/bc_classify_system.md               # cached Haiku system prompt body (verdict+topic enums)
supabase/migrations/<ts>_bc_monitor_rpcs_and_config.sql
        # bc_news_event_classify(), bc_thesis_update_upsert(), bc_failed_synthesis_upsert();
        # bc_config seeds (§2.6); grants to bc_scanner. Disk-first, then `supabase db push` (NOT MCP apply
        # for code-tracked DDL — feedback_mcp_apply_migration_discipline). Re-introspect grants after.
```
Reuse (do **not** modify): `orchestrator_runtime/client.py`, `orchestrator_runtime/pricing.py`,
`modal_workers/scanners/insider_form4_scanner.py` (signal source), `modal_workers/providers/polygon/*`
(options fetcher lives in the sibling plan), the deployed `bc_news_event_upsert` RPC.

---

## 6. TEST PLAN

Tests under `modal_workers/tests/` (pytest, matching repo convention). **No live Anthropic/Polygon calls** —
the Anthropic client is faked.

### 6.1 Unit — threshold + corroboration (pure, no LLM) — `test_bc_threshold.py`
- Each §2.2 predicate fires on a synthetic market-signal/news payload and is silent below threshold (reads
  defaults via a stubbed `bc_config`).
- **Corroboration matrix (the load-bearing test):**
  - lone `low`-tier `contradicts_thesis` ⇒ `max_action_allowed='monitor'` (cannot escalate alone).
  - `contradicts_thesis` + an insider_sell_cluster co-signal ⇒ `'investigate'`.
  - `contradicts_thesis` + two independent sources (distinct domains) ⇒ `'investigate'`.
  - one `primary`-tier 8-K `contradicts_thesis` alone ⇒ `'investigate'`.
  - options-unavailable forces ceiling `monitor` even with corroboration.
  - model returns `exit` ⇒ clamped to `investigate` (v1 cap) and `action_clamped_from='exit'` logged.
  - model returns `investigate` with `max_action_allowed='monitor'` ⇒ downgraded to `monitor`, logged.
- `stance_hint` table (§2.4) cutpoints.

### 6.2 Unit — contract validation/plausibility — `test_bc_contract.py`
- The §1.4 worked example **validates** against `bc_synthesis_v1.json`.
- Mutations each rejected with the right `failure_type`: unknown top-level key (`schema_violation`);
  `evidence_ref.id` not in provenance (`implausible`); `model_risk_band` ≠ score band (`implausible`);
  options-unavailable but `options_implied_move_pct` non-null (`implausible`); >3 bullets (`schema_violation`).
- `jsonschema` import-missing ⇒ raises (no silent pass).

### 6.3 Unit — LLM call structure (faked client) — `test_bc_llm.py`
- Haiku classify: fake client returns a fixed verdict/topic JSON; assert the system block carries
  `cache_control: ephemeral`, model is the pinned Haiku id, output persists via the classify RPC, cost is
  added to the budget accumulator.
- Sonnet synthesize: assert the schema is injected into the cached system block, `max_action_allowed` is in
  the user bundle, `temperature` absent, model is the pinned Sonnet id.
- Budget: configure a fake client whose accumulated cost crosses `l4.daily_budget_usd`; assert
  `BudgetExceededError` ⇒ run `status='failed'` with `reason='killed_budget'` / `log.kill='budget'`, partial
  cost recorded, no further calls, critical flag intent emitted. Assert `status` is a CHECK-valid token
  (never `killed_budget`).
- Dedup: 3 near-duplicate news rows ⇒ exactly 1 Haiku call; siblings inherit the verdict.

### 6.4 Integration — **seeded-delta end-to-end** (the Phase-2 exit-gate proof) — `test_bc_monitor_seeded_delta.py`
The gate from the high-level plan: *"seed a delta → assert one schema-valid `bc_thesis_updates` row is
produced, framed vs implied move."*
1. Seed (against a test schema or transactional fixture): one `bc_applications` row; a
   `bc_rubric_scores` row (`risk_band='elevated'`, percentile 78); today's `bc_market_signals`
   (`insider_cluster_buy` net +$2.1M; `options_iv` with `iv30_dod_pp=7`, `implied_move_pct_pdufa=14`); one
   classified `bc_news_events` row (`manufacturing_buildout`, `confirms_thesis`, tier `low`).
2. Run `run_daily.py` with a **fake Anthropic client** that returns the §1.4 example payload (so no network).
3. Assert: exactly **one** `bc_thesis_updates` row for `(app, today)`; `synthesis` validates against
   `bc_synthesis_v1.json`; `risk_vs_market.stance='market_underpricing_risk'` and references the implied
   ±14%; `recommended_action='monitor'` (corroboration ceiling held — the only escalation candidate was a
   lone low-tier news verdict); `trigger_reasons` ⊇ {`insider_buy_cluster`,`iv30_jump`,
   `commercial_plan_signal`}; `cost_usd` > 0; a `bc_pipeline_runs` row exists with `status='succeeded'`.
4. Negative: a second run the same day ⇒ no duplicate row (UNIQUE/idempotent).
5. Dry-run: set `l4.synthesis_dry_run=true` ⇒ **no** thesis row, but a `bc_pipeline_runs.log` "would_fire"
   entry for the name (the warm-up mode).
6. Implausible-escalation guard: feed a fake payload that sets `recommended_action='investigate'` with only
   the lone low-tier verdict ⇒ row persists with action **downgraded to `monitor`** and
   `action_clamped_from='investigate'` in the run log (proves the LLM cannot self-escalate).

### 6.5 Dry-run / warm-up (ops, not CI)
Per spec §A3 / step-6: run the monitor with `l4.synthesis_dry_run=true` for ~7 days to observe real
`trigger_reasons` rates, then tune the §2.6 `bc_config` thresholds before enabling the Sonnet fire. No code
change to flip — just the config dial.

---

## 7. Open dependencies / hand-offs (call out to the Phase-2 lead)

1. **Polygon options access (BLOCKER for the implied-move core).** `PolygonMarketData` today exposes only
   quote/historical/market_cap/adv — **no options/IV/straddle methods**. The implied-move stream + its
   `options_iv` market-signal payload (keys in §2.1) are a **sibling-fetcher deliverable**; until it lands,
   `streams_available.options=false` and the contract degrades to band-only with a `monitor` ceiling. The
   contract is built to make that degradation honest and visible, but the moat's headline framing is muted
   until options data exists. Confirm Polygon tier (options snapshots) before relying on it.
2. **Sibling fetchers** must emit the exact `signal_type`s + payload keys in §2.1 (insider types already
   match `insider_form4_scanner.py`; options + news payload contracts are agreed here).
3. **Migration 005** (`operator_flags` bc_ sources) should be applied before the monitor writes flags;
   until then flags route to `bc_pipeline_runs.log` (§4.3).
4. **`exit` authoring** is intentionally Python-capped to `investigate` in v1 (§2.3). One-line raise if
   Pedro wants model-authored `exit`, keeping the corroboration requirement.
5. The two new persist/classify RPCs + config seeds ship in **one disk-first migration** (`supabase db
   push`), not MCP `apply_migration` (code-tracked DDL discipline).
