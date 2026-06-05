# Phase 0 — Pending-universe + PDUFA source (THE GATE)

**Project:** Light v4 (BC-FDA monitor) — monitor-first FDA catalyst tool.
**Status:** detail plan, ready to build. **Author date:** 2026-06-03.
**Parent:** `/Users/Pico/.claude/plans/plan-the-high-level-peppy-shell.md` (Phase 0 brief).
**Supabase project:** `xvwvwbnxdsjpnealarkh` (ref also exposed via MCP as `xvwvwbnxdsjpnealarkh`).

> **REFRAME (2026-06-03, per `v4_redesign_direction.md` — supersedes the 3-way framing below).**
> This is **NOT a greenfield 3-way bake-off.** The pending universe **already largely exists** in
> `fda_regulatory_events` (event_type='pdufa', ~32 future-dated, 100% tickered) → the monitor can
> **bootstrap now** on that seed. `edgar_8k_pdufa.py` is already the Modal writer of those rows but leaves
> `event_date=NULL` by design. So Phase 0's real job is **HARDEN the existing ledger**: make the writer
> parse the PDUFA date inline (lift the parser from `modal_workers/scanners/fda_pdufa_pipeline.py::_parse_filing_for_pdufa`),
> add a daily discover/update/retire loop + dedup + application_number backfill. **Approach 1 is the lead;
> approaches 2 (paid calendar) and 3 (FDA-primary) collapse to a quick coverage *confirm*, not a full
> spike.** The benchmark below still applies — but as validation of the hardened ledger's coverage, not a
> source bake-off — and the hard GO/NO-GO softens because the tickered seed already proves an in-window
> universe exists.

> **One-sentence goal.** Prove we can produce, *daily and reproducibly*, the set of **pending,
> in-window, tradeable NDA/BLA names** with a real **PDUFA date** + BT/FT/AA designations — by
> comparing three sourcing approaches head-to-head on coverage / latency / cost, then recommending one
> and wiring it to populate `bc_applications` + `bc_application_features.pdufa_date` +
> `bc_company_tradeable`. **No trustworthy universe ⇒ no monitor ⇒ stop and reconsider.**

This is a **spike + benchmark + a thin production write path**, not a full build. The exit artifact is a
benchmark report **and** a working daily enumeration of the winning source. Everything downstream
(Phase 1 scoring, Phase 2 monitor) keys off the `bc_*` rows this phase writes.

---

## 0. Ground truth established during planning (build on these; do not re-derive)

These were verified live against `xvwvwbnxdsjpnealarkh` and by reading the code on 2026-06-03.

### 0.1 The schema is deployed, empty, and the gate logic already exists in SQL

`bc_applications`, `bc_application_features`, `bc_company_tradeable`, `bc_pipeline_runs` all exist and are
**empty (0 rows)**. The decisive discovery: **`bc_candidates` (the matview) already encodes the entire
universe gate.** Its definition (verified via `pg_get_viewdef`) computes:

- **G3 in-window:** `0 <= (pdufa_date - CURRENT_DATE) <= l3.window_days`.
- **G2 tradeable:** `market_cap_usd >= l3.min_market_cap AND avg_daily_volume_usd >= l3.min_adv AND (options_chain_exists OR borrow_available)`.
- **G1 active/watchlist:** score-band gate (Phase 1's job; out of scope here).
- The tradeable join is **`bc_company_tradeable.sponsor_cik = bc_application_features.sponsor_cik`**
  (CIK is the join key, *not* ticker), taking the latest snapshot per CIK.
- `latest_features` is **`DISTINCT ON (application_number) ... ORDER BY snapshot_date DESC, built_at DESC`** —
  i.e. the matview already expects **snapshot-versioned** feature rows and picks the newest.

**Implication for Phase 0:** we do not invent the universe shape. We populate the three input tables so
that `bc_candidates` lights up with `tier IN ('active','watchlist')` rows. Phase 0 is responsible for
**G2 + G3 inputs** (`pdufa_date`, `appl_type`, tradeability). G1 (score) is Phase 1; until then,
`bc_candidates` rows will be `gate1_failed` (no score) — that is expected, and Phase 0 measures the
**pre-G1 population** directly off `bc_application_features` + `bc_company_tradeable`, not the matview's
final `tier`.

### 0.2 Live gate parameters (from `bc_config`, verified)

| key | value | meaning |
|---|---|---|
| `l3.window_days` | **120** | in-window horizon (days to PDUFA) |
| `l3.min_market_cap` | **250,000,000** | $250M min market cap |
| `l3.min_adv` | **2,000,000** | $2M/day min avg dollar volume |
| `l3.tau_nda` | 0.30 | (Phase 1) active band p_crl threshold |
| `l3.tau_nda_watchlist` | 0.50 | (Phase 1) watchlist band threshold |

> **Note / flag for Pedro:** these `bc_config` tradeability floors ($250M / $2M ADV) are **stricter**
> than the existing v3 `curate_tradeable_filter_pass.py` floors ($215M / $500K ADV). Phase 0 should
> compute tradeability with the `bc_config` values (authoritative for `bc_candidates`) but **report both
> cuts** so we can see how many names the stricter floor drops. Do not silently adopt the looser v3 floor.

### 0.3 Table contracts (PK / UNIQUE, verified)

- **`bc_applications`** — PK `application_number`. Columns: `application_number, sponsor_cik,
  sponsor_name, appl_type, created_at`. This is the **registry** (one row per app, no snapshot dimension).
- **`bc_application_features`** — PK `id (uuid)`; **UNIQUE `(sponsor_cik, application_number,
  snapshot_date)`**. Carries `pdufa_date (date, nullable)`, `appl_type`, `review_priority`, `has_bt`,
  `has_ft`, `has_aa`, `submission_date`, `cycle_type (NOT NULL)`, `is_biosimilar_bla (NOT NULL)`,
  `as_of_date (NOT NULL)`, `snapshot_date (NOT NULL)`, `built_at (NOT NULL)`, plus the M14 feature
  columns (Phase 1 fills the rest). **Phase 0 only needs to populate the identity + `pdufa_date` +
  designations + a few NOT-NULLs; the M14 features stay NULL until Phase 1.**
  > **`feature_quality` CHECK ∈ {standard, low, built_at_install}** (default `'standard'`; verified live
  > 2026-06-03 via `bc_application_features_feature_quality_check`). Phase 0 writes **`'low'` for surrogate-
  > appno rows** (`application_number LIKE 'EDGAR8K:%'`) and **`'standard'` otherwise**; the `'phase0_*'`
  > provenance tokens an earlier draft proposed are NOT allowed and would 23514-fail every upsert. Surrogate
  > provenance is carried durably by the `EDGAR8K:` appno prefix instead — Phase 1 re-derives this column
  > from coverage on the shared snapshot row (Phase 1 §5.2/§6), so `feature_quality` is not a stable
  > provenance channel. (The sibling `appl_type`/`review_priority` CHECKs are satisfied by Phase 0's
  > NDA/BLA + nullable-priority writes; `cycle_type` has no CHECK.)
- **`bc_company_tradeable`** — PK `id (uuid)`; **UNIQUE `(sponsor_cik, snapshot_date)`**. Columns:
  `sponsor_cik, ticker, snapshot_date, market_cap_usd, avg_daily_volume_usd, options_chain_exists,
  borrow_available, borrow_cost_bps, data_source, fetched_at`.
- **`bc_pipeline_runs`** — `id, pipeline_name, started_at, finished_at, status, snapshot_date,
  n_processed, n_failed, cost_usd, log (jsonb), reason`. **Every Phase 0 cron MUST open+close a row here**
  (fail-loud principle). **`status` CHECK ∈ {running, succeeded, failed, partial}** (verified live
  2026-06-04 via `bc_pipeline_runs_status_check`): open `'running'`, close `'succeeded'|'partial'|'failed'`
  — NOT `'ok'`/`'error'` (they 23514-fail).

> Idempotency pattern to use everywhere: `client._rest_with_retry("POST", "<table>?on_conflict=<cols>",
> json_body=[...], prefer="resolution=merge-duplicates,return=minimal")` — exactly the pattern in
> `supabase_client.py` and `openfda_ingest._upsert_application_submissions`.

### 0.4 PDUFA-date reality today (the problem we are solving)

- `fda_regulatory_events` has **91** `event_type='pdufa'` rows. Of these, **53 are `source_feed='edgar_8k_pdufa'`
  with `event_date = NULL` 100% of the time** (the fetcher deliberately punts the date — confirmed in
  `edgar_8k_pdufa.py` lines 24-27, 373: `"event_date": None`). The 53 rows resolve to only **16 distinct
  CIKs/tickers** → the 8-K stream is **empirically large/mid-cap-skewed** (only names already in
  `fda_assets`).
- The other **38** pdufa rows have **`source_feed = NULL`** (operator-script-fed, the frozen
  `fda_regulatory_events` cohort) and DO carry `event_date` (33 future-dated). This is the **hand-seeded**
  ledger — small, stale, not a daily source.
- `catalyst_universe` has **no `pdufa` catalyst_type** (types present: `fda_approval`=1189,
  `mna_announce`, `mna_close`, `adcomm`=26, plus one archive backfill). The 1189 `fda_approval` rows are
  `openfda_drugsfda`-fed, dated 2026-04-13..2026-05-29, but have **`distinct_tickers=1`** (ticker
  resolution is essentially absent) and **no `application_number` column** — so they are a usable
  **approval-date** ground-truth proxy but require sponsor→appl cross-join to be keyed.
- `fda_assets.next_catalyst_date` populated on **17/157** rows — not a source.
- **openFDA `drug/drugsfda` does NOT carry pending PDUFA targets.** It exposes only *post-decision*
  submissions (`submission_status_date` with AP/TA/CR statuses). `submission_status:RL` 404s (memory
  `openfda_drugsfda_no_crl_status.md`). **This is the central reason approach 3 (FDA primary) cannot give
  a forward PDUFA date directly** — note it explicitly in the benchmark.

### 0.5 A working PDUFA-date extractor ALREADY EXISTS in the repo (decisive for approach 1)

`modal_workers/scanners/fda_pdufa_pipeline.py` already does **EDGAR 8-K auto-discovery + regex date
extraction**:
- `_discover_pdufa_from_edgar()` (line 365) — EFTS search for PDUFA 8-Ks.
- `_parse_filing_for_pdufa(file_id, cik, adsh, user_agent)` — fetches filing body and regex-extracts
  **(date_iso, drug_name_candidate)**; thin shim `_extract_pdufa_date_from_filing()` (line 524) returns
  just the date.
- Backed by `modal_workers/shared/edgar_efts.py`: `efts_search(query, date_from, date_to, forms, size,
  user_agent)` and `fetch_filing_text(file_id, cik, adsh, user_agent)` (line 77, "for downstream regex
  extraction (PDUFA date, ...)").

So **approach 1 is "lift `_parse_filing_for_pdufa` out of the scanner, harden the regex, add designation
extraction, write to `bc_*`"** — *not* a from-scratch parser. This materially lowers approach-1 cost/risk
and should be reflected in the benchmark scoring.

### 0.6 Tradeability sourcing is solved by Polygon (verified)

`POLYGON_API_KEY` is the only price secret in the codebase (Modal `scanner-secrets::POLYGON_API_KEY`).
The providers already give us everything `bc_company_tradeable` needs:
- **`modal_workers/providers/polygon/market_data.py`** — `get_market_cap(ticker)` (`/v3/reference/tickers/{T}`
  → `results.market_cap`) and `get_adv(ticker, days=30)` (computes mean daily `close*volume` over a window
  = **dollar ADV**, exactly `avg_daily_volume_usd`).
- **`modal_workers/providers/polygon/options_data.py`** — `get_chain(ticker)` → if it returns a non-empty
  chain, `options_chain_exists = True`. (`get_event_window_liquidity` is a richer Phase-2 input; for
  Phase 0 a non-empty chain is sufficient for the boolean.)
- `borrow_available` / `borrow_cost_bps`: **Polygon does not expose borrow.** Phase 0 sets
  `borrow_available = NULL` (not False) and `data_source='polygon'`. The matview's G2 already tolerates
  this: `(options_chain_exists OR borrow_available)` passes on options alone. Document borrow as a known
  gap (a later short-locate feed can fill it).
- Auth/host: `modal_workers/providers/polygon/base.py` (`PolygonClient`, `apiKey` query param, retry).

### 0.7 Sponsor→ticker→CIK resolution exists

`modal_workers/shared/sponsor_resolver.py` (`resolve_sponsor(name, client, skip_jaccard=)`) maps sponsor
name → ticker/mic/country/tradeable via a curated map + Jaccard fallback (already used by
`openfda_ingest`). `entity_identifiers` (`id_type='cik'`) maps CIK↔entity (used by `edgar_8k_pdufa`'s
`_resolve_asset_id`). **CIK is the spine** for `bc_*` (it is `sponsor_cik` on all three tables and the
matview join key). For an 8-K hit we already have the **filer CIK** directly from EFTS `display_names`
(`_extract_cik` in `edgar_8k_pdufa.py`) — no resolution needed. Ticker for Polygon comes from the EFTS
parenthetical (`_extract_ticker`) or `entity_identifiers`/`resolve_sponsor`.

---

## 1. The three approaches — concrete specs

For each: exact endpoints/parsers, **what it can and cannot yield (esp. real PDUFA _date_ vs mere
mention)**, and expected coverage skew.

### Approach 1 — EDGAR 8-K extraction (extend `edgar_8k_pdufa.py`, lift the existing regex)

**Endpoint(s):**
- EFTS discovery: `https://efts.sec.gov/LATEST/search-index?forms=8-K&q="PDUFA goal date" | "PDUFA action
  date" | "PDUFA target action"&dateRange=custom&startdt=&enddt=&from=` (already in `edgar_8k_pdufa.py`,
  with retry on 429/5xx).
- Filing body: `efts_search()` + `fetch_filing_text(file_id, cik, adsh, user_agent)` from
  `modal_workers/shared/edgar_efts.py`.

**Parser:** reuse/extract `_parse_filing_for_pdufa()` from `fda_pdufa_pipeline.py` → returns
`(pdufa_date_iso, drug_name_candidate)`. **Harden** for the BC path:
- Anchor the date regex to a PDUFA context window (within ~200 chars of "PDUFA"/"goal date"/"action
  date") to avoid grabbing an unrelated date.
- Accept formats: `Month DD, YYYY` (`%B %d, %Y`), `MM/DD/YYYY`, ISO. Normalize to ISO.
- **Add designation extraction** (new, small): regex/keyword scan of the same filing body for
  "Breakthrough Therapy" → `has_bt`, "Fast Track" → `has_ft`, "Accelerated Approval" → `has_aa`. These are
  *best-effort booleans from the 8-K text* (an 8-K announcing a PDUFA date frequently restates
  designations). Where absent, leave NULL (do not write False — NULL = "unknown", matching
  `feature_assembly._designations` semantics which read `extensions['designations']`).
- **appl_type:** 8-Ks rarely state the NDA/BLA number. Strategy: (a) if the filer's CIK/drug maps to an
  existing `fda_assets`/`fda_application_submissions` row, borrow `application_number` + `appl_type`;
  (b) else **synthesize a stable surrogate application_number** `EDGAR8K:<cik>:<drug_slug>` and set
  `appl_type` by keyword (BLA if "biologics license"/"BLA" present, else NDA default). Surrogate rows
  are identifiable durably by their `application_number` prefix `EDGAR8K:` (and carry the CHECK-allowed
  `feature_quality='low'` token) so Phase 1 can treat them as lower-confidence. (This is a deliberate
  compromise — see risks.)

**Can yield:** a **real PDUFA date** (the core win) + drug name + filer CIK/ticker + designations (partial)
+ filing date (= disclosure latency anchor). **Cannot reliably yield:** the FDA application number /
appl_type with certainty; designations when the 8-K omits them; anything for **private/foreign filers or
names that never file an 8-K disclosing the goal date**.

**Coverage skew (measured):** large/mid-cap US-listed sponsors who issue IR-grade 8-Ks. Empirically the
existing 53 rows → 16 distinct names. Expect **high precision, moderate recall**, skewed away from
micro-caps and foreign (6-K) issuers. 6-K inclusion (`forms="8-K,6-K"`) can widen recall — test it.

**Latency:** **excellent** — the 8-K is filed within ~4 business days of the FDA assigning the date, often
the same/next day. This is typically the *earliest public* structured disclosure of a goal date.

**Cost:** **~$0** (SEC EFTS is free; requires `SEC_USER_AGENT`). Polite-rate at 10 req/s (existing).

### Approach 2 — Third-party biopharma catalyst calendar

**Candidates:** BioPharmaCatalyst (`biopharmacatalyst.com`), RTTNews biotech calendar, Evaluate Pharma,
"FDA Calendar"-type aggregators, Nasdaq/Benzinga FDA calendars.

**Assessment performed during planning:**
- `biopharmacatalyst.com/calendars/fda-calendar` returned **HTTP 404 to an unauthenticated fetch** — the
  public calendar is **not openly machine-readable** at that path (login/JS-gated or moved). Consistent
  with `edgar_8k_pdufa.py`'s own header note: "curated databases (BioPharma Catalyst, etc.) are paid +
  scraping-hostile."
- No public, documented, free API was found for any of these during planning.

**Spec (what the spike must do, lightly):** for each candidate, **do not build a scraper**. Instead spend
≤1–2h confirming: (a) is there a public/CSV/RSS/API surface returning PDUFA date + ticker + designation;
(b) what are the **ToS / robots.txt** constraints on automated access; (c) **pricing** for a sanctioned
API. Capture findings (URL, access mode, cost, ToS verdict) in the benchmark report. If one offers a free
or cheap sanctioned API with a real PDUFA *date*, pull a one-shot sample for the benchmark cohort only
(manual, not a cron).

**Can yield:** in principle the **cleanest** dataset — PDUFA date + ticker + drug + designation + indication,
including small-caps the 8-K stream misses. **Cannot yield (practically):** a free, ToS-clean, reproducible
*daily* feed — the whole category is paywalled/scraping-hostile. Treat approach 2 as a **coverage
yardstick** (how many names a paid feed would add) more than a buildable daily source, unless a sanctioned
API materializes.

**Coverage skew:** broadest (their business is completeness), but legally/operationally gated.
**Latency:** good (curated daily). **Cost:** the differentiator — **paid** ($/mo subscription or per-call
API), plus ToS risk if scraped. The monitor-first thesis is "near-zero marginal cost," so a paid feed is
a thesis-relevant cost line, not a free win.

### Approach 3 — FDA primary (Drugs@FDA + Federal Register + AdComm calendars + inference)

**Endpoints:**
- openFDA `drug/drugsfda.json` (via `openfda_client`) — **post-decision only.** Gives approved/tentative
  dates, sponsor, products, submission history. **Does not carry a forward PDUFA goal date.** (Verified;
  `submission_status:RL` 404s.) Useful only to (a) resolve sponsor↔appl_number↔appl_type and (b)
  retire/confirm a pending date once a decision posts.
- Federal Register API `https://www.federalregister.gov/api/v1/documents.json` — **openly accessible JSON**
  (verified), fields incl. title/abstract/agencies/publication_date/html_url. Reuse `fed_register_adcom.py`
  + `modal_workers/shared/fda_advisory_calendar`. **AdComm meeting notices give a meeting date, NOT the
  PDUFA goal date** (the PDUFA date is typically weeks after the AdComm). It is a *proximity hint*, not the
  date. A FedReg search for "PDUFA action date" returned procedural notices with **no concrete dates**
  (verified) — confirming FDA-primary date inference is weak.
- FDA AdComm calendar pages / `fda_adcomm_pdufa.py` (which despite the name pulls `drug/drugsfda`
  approvals, not forward dates).

**Inference layer (what "FDA primary" really requires):** to get a forward PDUFA date from FDA-primary
sources you must **infer**: submission acceptance/filing date (from a press release or FedReg) + the PDUFA
clock (10 months standard review, 6 months priority from the 60-day filing date) → estimated goal date.
This yields a **±weeks estimate**, not the company-disclosed exact date.

**Can yield:** sponsor/appl_number/appl_type with authority (drugsfda); AdComm dates; an **inferred**
PDUFA window. **Cannot yield:** the *exact disclosed* PDUFA date for a pending app (no FDA primary source
publishes a forward goal-date calendar). 

**Coverage skew:** broad on *approved* history; **poor on pending forward dates** without inference; AdComm
covers only the subset of apps that get a committee meeting. **Latency:** AdComm notices lead the meeting;
drugsfda lags the decision. **Cost:** ~$0 (openFDA needs `OPENFDA_API_KEY` for the 120k/day cap; FedReg
free). Mind the openFDA 1,000/day shared-IP cap (memory `openfda_rate_limit_gap.md`).

### 1.x Summary expectation (to be confirmed by the benchmark)

| | real PDUFA *date*? | coverage | latency | cost | reproducible daily? |
|---|---|---|---|---|---|
| **1 EDGAR 8-K** | **Yes** (extracted) | mid/large-cap US | **best** | ~$0 | **yes** |
| **2 3rd-party** | Yes (if sanctioned API) | **broadest** | good | **paid / ToS-risk** | only if API |
| **3 FDA primary** | **No** (only inferred ±wks) | approved-heavy | mixed | ~$0 | yes (but dates weak) |

**Prior going in (state it, let the benchmark confirm):** approach 1 is the likely winner for v1 (real
dates, free, reproducible, and a working extractor already exists), with approach 3 as a **corroboration/
appl_type-resolution sidecar**, and approach 2 reserved as a paid coverage-booster if/when the thesis
justifies the cost.

---

## 2. Benchmark methodology

### 2.1 Ground-truth cohort (hand-checkable)

We need a set of **known PDUFA catalysts with a known date** to score recall/precision against. Build it
from three converging sources, then **hand-verify each row** (this is the crux — a noisy truth set
invalidates the benchmark):

1. **Recently-resolved approvals worked backward (primary).** `catalyst_universe` `fda_approval` rows
   (1189, last ~6 weeks, `openfda_drugsfda`-fed) give **approval dates** for real drugs. For an approved
   NDA/BLA, the PDUFA goal date ≈ the approval date (often the FDA acts on/near the goal date). Take a
   sample of ~30–40 recent approvals of **tradeable, US-listed** sponsors and, for each, **look up the
   actual disclosed PDUFA date** from the sponsor's prior 8-K / press release (manual, one-time). This
   yields a truth set of `(ticker, drug, appl_number, true_pdufa_date)`.
2. **The 38 operator-seeded dated pdufa rows** (`fda_regulatory_events`, `source_feed=NULL`, 33
   future-dated). These are already-curated pending PDUFA dates — treat as **truth for pending names**
   after a quick sanity re-check of each date against the company's latest disclosure.
3. **A small public list** for breadth/cross-check: a hand-collected list of ~15–25 well-known
   **near-term** PDUFA dates (next ~120 days) from public reporting (e.g. recent biotech-press roundups).
   Used mainly to test recall on names outside the operator set.

**Target truth-set size:** ~40–60 catalysts spanning **both** resolved (to test the extractor end-to-end on
filings we know exist) and **pending in-window** (to test the live universe). Store it as a checked-in
fixture: `modal_workers/fetchers/universe/testdata/bc_pdufa_truthset.json`
(`[{ticker, cik, drug, appl_number?, appl_type?, true_pdufa_date, designations?, source, market_cap_bucket}]`).

> **Caveat to honor (from memory `bc_fda_tool_review_2026-06-03.md`):** the prior `eval_harness` CRL
> cohort was exhaust (45 rows → ~12 usable, 26 = one Axsome event). Do **not** reuse it as truth here. The
> truth set above is independently hand-built and **must be diversified across sponsors** (cap any single
> sponsor at ≤2 rows) and across market-cap buckets so coverage skew is measurable, not hidden.

### 2.2 Metrics (per approach, computed against the truth set)

For each approach, run its enumerator over the same historical window the truth set spans and compute:

- **Coverage / recall** = (# truth catalysts the approach surfaced) / (# truth catalysts). Break down by
  **market-cap bucket** (micro <$250M / small $250M–2B / mid+ >$2B) and by **status** (pending vs
  resolved) — this exposes the 8-K large-cap skew quantitatively.
- **Date accuracy** = of surfaced catalysts, fraction whose extracted date is **exactly correct**, and
  the distribution of |extracted − true| in days (0 / ≤7 / ≤30 / >30). Approach 3's inference will live in
  the ≤30/>30 buckets; approach 1 should be mostly exact.
- **Precision / false-positive rate** = of rows the approach emitted, fraction that are spurious (no real
  catalyst, or wrong drug). Important for 8-K regex over-matching.
- **Latency** = for resolved catalysts, days between **earliest public disclosure** of the goal date and
  the approach surfacing it. For 8-K, the disclosure *is* the filing → latency ≈ filing-to-detect (≈0 if
  cron runs daily). For inference, latency from acceptance press release.
- **Cost** = $/month at steady state (API subscription for #2; ~$0 for #1/#3) + **ToS verdict**
  (clean / gray / prohibited) + Polygon/openFDA call volume implied.
- **Reproducibility** = can it run unattended daily without a human/JS/login? (boolean + notes).

### 2.3 Declaring a winner (decision rule)

Score with a simple weighted rubric reflecting the monitor-first thesis (digest of ~20 names, near-zero
cost, fail-loud daily):

```
winner_score = 0.35*recall_in_window      # do we SEE the pending names?
             + 0.25*date_exact_rate        # is the date trustworthy?
             + 0.15*(1 - false_pos_rate)
             + 0.15*reproducible_daily      # 1.0 if unattended-daily, else 0
             + 0.10*cost_score              # 1.0 if ~$0 & ToS-clean, →0 as $/ToS-risk rises
```

Recommend the **single highest-scoring approach as the v1 source**, and explicitly state the best
**sidecar** (e.g. "Approach 1 primary for dates; Approach 3 drugsfda join for appl_number/appl_type and
for retiring resolved dates"). Approach 2 is recommended **only** if it both wins on recall *and* clears a
ToS/cost bar Pedro accepts. The deliverable names the winner **and** quantifies what each rejected
approach would have added (so the choice is auditable).

---

## 3. Enumeration → write contract (the winning source populates `bc_*`)

Regardless of winner, the write path into the three tables is identical; only the *date/designation
producer* differs. All writes are **idempotent upserts** and **snapshot-versioned** where the table has a
snapshot dimension.

### 3.1 `bc_applications` (registry — one row per app)

```
on_conflict = application_number
row = {
  application_number,             # real NDA/BLA number if known, else surrogate "EDGAR8K:<cik>:<drug_slug>"
  sponsor_cik,                    # spine; from EFTS filer CIK (approach 1) or drugsfda+resolver (approach 3)
  sponsor_name,
  appl_type,                      # 'NDA' | 'BLA' (best-effort; BLA if biologics keywords present)
}
prefer = "resolution=merge-duplicates,return=minimal"
```

### 3.2 `bc_application_features` (snapshot-versioned; carries `pdufa_date` + designations)

`snapshot_date = today (UTC)`. Upsert on the composite UNIQUE so a daily re-run updates today's row and
keeps history across days (which `bc_candidates.latest_features` then reads newest-first).

```
on_conflict = sponsor_cik,application_number,snapshot_date
row = {
  sponsor_cik, application_number, appl_type,            # identity
  pdufa_date,                                            # THE payload (real or, approach 3, inferred)
  has_bt, has_ft, has_aa,                                # designations (NULL when unknown — not False)
  review_priority,                                       # 'PRIORITY'|'STANDARD' if derivable, else NULL
  submission_date,                                       # if known, else NULL
  cycle_type:        'unknown',                          # NOT NULL — placeholder until Phase 1
  is_biosimilar_bla: false,                              # NOT NULL — default false
  as_of_date:        today,                              # NOT NULL
  snapshot_date:     today,                              # NOT NULL
  built_at:          now(),                              # NOT NULL
  feature_quality:   'low' if application_number LIKE 'EDGAR8K:%' else 'standard',  # CHECK-allowed ∈ {standard,low,built_at_install}
                     #   surrogate-vs-real provenance lives on the EDGAR8K: appno prefix, NOT here:
                     #   Phase 1 re-derives feature_quality from coverage on this same snapshot row (Phase 1 §5.2/§6).
                     #   The earlier 'phase0_universe'/'phase0_surrogate_appl' tokens 23514-fail the live CHECK.
  # all M14 feature columns (n_prior_filings, n_8ks_..., etc.) left NULL — Phase 1 fills them
}
prefer = "resolution=merge-duplicates,return=minimal"
```

> The matview's `latest_score` join requires a matching `application_number` in `bc_rubric_scores` to
> surface a score; Phase 0 deliberately leaves scores absent, so `bc_candidates.tier='gate1_failed'` for
> these rows. **Phase 0's success metric reads `bc_application_features` directly** (count of distinct
> in-window apps with non-NULL `pdufa_date` whose sponsor passes G2), not `tier='active'`.

### 3.3 `bc_company_tradeable` (Polygon; snapshot-versioned per CIK)

For each **distinct `sponsor_cik`** in today's universe, resolve a ticker (EFTS parenthetical →
`entity_identifiers` → `resolve_sponsor`), then call Polygon:

```
on_conflict = sponsor_cik,snapshot_date
row = {
  sponsor_cik,
  ticker,
  snapshot_date:        today,
  market_cap_usd:       PolygonMarketData.get_market_cap(ticker),
  avg_daily_volume_usd: PolygonMarketData.get_adv(ticker, days=30),   # dollar ADV
  options_chain_exists: bool(PolygonOptionsData.get_chain(ticker)),   # non-empty chain
  borrow_available:     NULL,                                         # Polygon has no borrow → unknown
  borrow_cost_bps:      NULL,
  data_source:          'polygon',
  fetched_at:           now(),
}
prefer = "resolution=merge-duplicates,return=minimal"
```

Caching: pass `cache_prefix='polygon'` per `base.py` (market cap / reference 7d TTL, quotes 1h) to stay
well under Polygon rate limits across the ~tens-of-names universe. Build providers once per run
(`PolygonClient` reuse) to share the per-instance caches.

### 3.4 Confirmation: Polygon supplies market cap + ADV — **yes** (see §0.6)

`get_market_cap` (from `/v3/reference/tickers`) and `get_adv` (dollar ADV from daily aggregates) are
already implemented and unit-tested (`test_polygon_providers.py`). No new provider needed. `borrow_*`
is the only `bc_company_tradeable` field with no source; it is **non-blocking** because G2 accepts options
liquidity alone.

---

## 4. GO / NO-GO exit gate

Run the winning enumerator for the **current** window and evaluate against live `bc_*` state.

### 4.1 PASS (GO) criteria — all must hold

1. **Universe size:** **≥ 15** (target 15–20) **distinct in-window pending NDA/BLA `application_number`s**
   in `bc_application_features` with **non-NULL `pdufa_date`** where `0 ≤ pdufa_date − today ≤ 120` and
   `appl_type IN ('NDA','BLA')`.
2. **Tradeability:** of those, **≥ 12** have a `bc_company_tradeable` row for the same `sponsor_cik`
   passing **G2** (`market_cap_usd ≥ $250M AND avg_daily_volume_usd ≥ $2M AND (options_chain_exists OR
   borrow_available)`). (i.e. the post-G2 in-window count is the real product universe.)
3. **Date trust:** on the benchmark truth set, **date-exact rate ≥ 0.80** and **false-positive rate ≤
   0.15** for the winning approach.
4. **Reproducibility / fail-loud:** the enumerator runs unattended as a Modal cron, writes a
   `bc_pipeline_runs` row every run (status `succeeded|partial|failed`, `n_processed`, counts), and a second
   same-day run is a clean idempotent no-op (no dup rows; verified via the composite UNIQUEs).
5. **Cost:** steady-state marginal cost ≈ $0 (approach 1/3) — or, if approach 2 won, a cost line Pedro has
   explicitly accepted.

Verification query for criteria 1–2 (post-build):
```sql
WITH f AS (
  SELECT DISTINCT ON (application_number) application_number, sponsor_cik, appl_type, pdufa_date
  FROM bc_application_features
  ORDER BY application_number, snapshot_date DESC, built_at DESC
), t AS (
  SELECT DISTINCT ON (sponsor_cik) sponsor_cik, market_cap_usd, avg_daily_volume_usd,
         options_chain_exists, borrow_available
  FROM bc_company_tradeable ORDER BY sponsor_cik, snapshot_date DESC
)
SELECT
  count(*) FILTER (WHERE f.appl_type IN ('NDA','BLA') AND f.pdufa_date IS NOT NULL
                   AND (f.pdufa_date - CURRENT_DATE) BETWEEN 0 AND 120)             AS in_window,
  count(*) FILTER (WHERE f.appl_type IN ('NDA','BLA') AND f.pdufa_date IS NOT NULL
                   AND (f.pdufa_date - CURRENT_DATE) BETWEEN 0 AND 120
                   AND COALESCE(t.market_cap_usd,0) >= 250000000
                   AND COALESCE(t.avg_daily_volume_usd,0) >= 2000000
                   AND (COALESCE(t.options_chain_exists,false) OR COALESCE(t.borrow_available,false))) AS in_window_tradeable
FROM f LEFT JOIN t ON t.sponsor_cik = f.sponsor_cik;
```

### 4.2 FAIL (NO-GO) — and what "reconsider the monitor-first thesis" means

If **no** approach reaches ≥15 in-window names at ≥0.80 date-exact **cheaply** (≈$0/ToS-clean), the
monitor-first thesis is in question, because the moat ("fast daily synthesis on ~20 tradeable names") has
no trustworthy universe to stand on. Escalate to Pedro with the benchmark numbers and these branches:

- **Universe too small but dates trustworthy** (e.g. only 8–12 in-window tradeable names from 8-K alone):
  the thesis may still hold at *reduced scope* (a 10-name monitor) — decide whether 10 names is worth the
  build, or whether to **buy** approach 2 to reach 20 (turns "near-zero cost" into "small fixed cost").
- **Dates untrustworthy** (8-K recall fine but date-exact <0.80, or only approach 3's ±weeks inference):
  monitor-vs-implied-move framing breaks (you can't position around a date you don't trust). **Reconsider**
  = either invest in a paid exact-date feed (#2) or **pivot the product** away from date-anchored
  monitoring toward event-driven (react to 8-K/CRL as they post, no forward calendar).
- **Only approach 2 works but is paid/ToS-blocked:** the "zero marginal cost" premise is false →
  re-evaluate whether the edge justifies a data subscription before any further build.

The NO-GO report must state, per approach, the exact recall/date/cost numbers so the reconsideration is a
data decision, not a vibe.

---

## 5. Files to create / modify, Modal cron wiring, risks, test plan

### 5.1 New files

| Path | Purpose |
|---|---|
| `modal_workers/fetchers/universe/bc_universe_pdufa.py` | **Primary deliverable.** The winning enumerator. Discovers pending PDUFA apps (approach 1 lift: `efts_search` + hardened `_parse_filing_for_pdufa` + designation scan), resolves CIK/ticker, calls Polygon, and writes `bc_applications` + `bc_application_features` + `bc_company_tradeable` idempotently. Opens/closes a `bc_pipeline_runs` row. CLI `--start-date/--end-date/--apply` mirroring `edgar_8k_pdufa.py`. |
| `modal_workers/shared/bc_pdufa_extract.py` | Pure (no-I/O) PDUFA-date + designation parser extracted/hardened from `fda_pdufa_pipeline._parse_filing_for_pdufa`, so it is unit-testable against fixtures. Used by the enumerator. |
| `modal_workers/fetchers/universe/testdata/bc_pdufa_truthset.json` | Hand-built benchmark cohort (§2.1). |
| `modal_workers/scripts/bc_phase0_benchmark.py` | One-shot: runs each approach's enumerator over the truth-set window, computes the §2.2 metrics, prints the §2.3 rubric + the §4 GO/NO-GO verdict. Read-only against live DB; does NOT need `--apply`. **Outputs the benchmark report** (stdout + optional JSON). |
| `modal_workers/tests/test_bc_pdufa_extract.py` | Unit tests for the extractor (date formats, context-anchoring, designation booleans, non-match → None). |
| `modal_workers/tests/test_bc_universe_pdufa.py` | Write-contract tests with a fake Supabase client (assert upsert bodies, on_conflict targets, NOT-NULL placeholders, NULL-not-False designations/borrow, `bc_pipeline_runs` open/close). |

### 5.2 Modified files

| Path | Change |
|---|---|
| `modal_workers/app.py` | Register `bc_universe_pdufa` as a Modal function (image, `secrets=[scanner_secrets, supabase_secrets]` — `scanner_secrets` carries `SEC_USER_AGENT`, `OPENFDA_API_KEY`, `POLYGON_API_KEY`). Wire into the daily dispatcher. **Two viable wirings:** (a) add to `public.scanners` with `cadence='daily'` + a `scheduled_hour_utc` and let `dispatch_release_times` pick it up registry-style (preferred — retiming is a DB UPDATE, matches the codebase pattern); OR (b) add the name to `_FETCHERS_AT_HOUR[<hour>]` in `app.py`. Use (a). Pick an early UTC hour (e.g. 11:00 UTC, after US 8-K filings settle) — note this is a **universe-build** cron, distinct from the Phase 2 monitor cron. |
| `public.scanners` (DB row, not code) | INSERT a `bc_universe_pdufa` row: `cadence='daily'`, `status='operational'`, `scheduled_hour_utc=11`, sensible timeouts, `default_scoring_profile` N/A (it's a fetcher). Done via the existing scanner-registry convention (memory `scanner_registry_vs_db.md`: the DB row is authoritative, not the JSON). |

> **No new migrations.** All target tables exist. (Migration 005, `operator_flags` bc_ sources, stays
> unapplied per the high-level plan — Phase 0 uses `bc_pipeline_runs`, not `operator_flags`, for liveness.)

### 5.3 Risks

1. **8-K coverage skew → universe < 15.** Mitigation: include `forms="8-K,6-K"`; widen the discovery
   window to ≥120d back-and-forward (the goal date can be disclosed months ahead); and **add approach 3's
   AdComm/FedReg + the 38 operator-seeded dated rows as a backfill** so the v1 universe is a *union*
   (8-K primary + operator/AdComm sidecar) rather than 8-K alone. The benchmark will quantify how much the
   union adds.
2. **appl_number / appl_type uncertainty from 8-Ks.** Surrogate `application_number` (`EDGAR8K:<cik>:<slug>`)
   keeps the pipeline moving but pollutes the registry. Mitigation: prefer joining to
   `fda_application_submissions` / `fda_assets` by CIK+drug to recover the real number; identify surrogates
   by the durable `EDGAR8K:` appno prefix and set `feature_quality='low'` (CHECK-allowed) so Phase 1
   down-weights them; reconcile surrogate→real when drugsfda later exposes the approval. **Flag to Pedro**
   as an accepted v1 compromise.
3. **Regex date false positives** (grabbing a non-PDUFA date). Mitigation: context-anchor (±200 chars of a
   PDUFA token), require a PDUFA keyword in the same sentence, unit-test against adversarial fixtures, and
   measure false-positive rate in the benchmark (gate ≤0.15).
4. **Designation booleans are weak from 8-K text.** Mitigation: NULL (not False) when absent; Phase 1's
   `feature_assembly._designations` already prefers `extensions['designations']` evidence rows, so this is
   a soft input, not load-bearing for the universe.
5. **Polygon ticker mismatch / missing market cap** for thin biotech tickers. Mitigation: `get_market_cap`
   returns None gracefully; such names fail G2 (correctly excluded). Log Polygon misses in
   `bc_pipeline_runs.log` so coverage loss is visible.
6. **openFDA 1,000/day shared-IP cap** if approach 3's drugsfda sidecar paginates hard (memory
   `openfda_rate_limit_gap.md`). Mitigation: ensure `OPENFDA_API_KEY` is set (120k/day) and route via
   `openfda_client`; keep the sidecar query narrow (sponsor/appl-scoped, not full sweeps).
7. **Truth-set contamination** (the prior eval_harness failure). Mitigation: hand-verify every truth row,
   cap any single sponsor at ≤2 rows, diversify market-cap buckets; document provenance per row.
8. **Two PDUFA writers diverge** (`edgar_8k_pdufa.py` → `fda_regulatory_events` vs new
   `bc_universe_pdufa` → `bc_*`). These are *different ledgers by design* (memory
   `catalyst_universe_vs_fda_regulatory_events.md` pattern). Document that `bc_*` is the BC monitor's
   source of truth; do not try to bridge them in Phase 0.

### 5.4 Test plan

- **Unit (offline, fixtures):** `test_bc_pdufa_extract.py` — feed saved 8-K body snippets (one per date
  format, one with each designation, one with a decoy date, one with none); assert `(date_iso, drug,
  {bt,ft,aa})`. `test_bc_universe_pdufa.py` — fake client asserts upsert shapes / on_conflict /
  NOT-NULL placeholders / NULL-not-False / pipeline-run open+close.
- **Integration (dry-run, live read-only):** run `bc_universe_pdufa.py --start-date … --end-date …`
  WITHOUT `--apply` against live EFTS + Polygon; assert it produces ≥15 in-window candidate rows in-memory
  and logs per-name market cap/ADV/options. No DB writes.
- **Benchmark:** `bc_phase0_benchmark.py` over the truth-set window → prints recall/date-accuracy/FP/
  latency/cost per approach + the rubric winner + GO/NO-GO verdict. **This is the phase deliverable.**
- **Idempotency:** `--apply` once, snapshot row counts; `--apply` again same day; assert counts unchanged
  on the composite UNIQUEs (and a *new* `snapshot_date` the next day adds, not replaces, history).
- **Fail-loud:** force an exception mid-run (e.g. bad SEC_USER_AGENT) and assert a `bc_pipeline_runs` row
  lands with `status='failed'` + `reason` — liveness must survive failure.
- **Gate query:** run the §4.1 SQL post-`--apply`; record `in_window` / `in_window_tradeable` in the
  report and check against the ≥15 / ≥12 thresholds.

---

## 6. Build order (so the engineer can start immediately)

1. Extract + harden the parser → `bc_pdufa_extract.py` (+ unit tests). Lift from
   `fda_pdufa_pipeline._parse_filing_for_pdufa`; add context-anchoring + designation scan.
2. Build `bc_universe_pdufa.py` (approach 1 path): EFTS discover → parse → CIK/ticker resolve → Polygon
   tradeability → idempotent `bc_*` writes → `bc_pipeline_runs`. Dry-run first.
3. Hand-build `bc_pdufa_truthset.json` (§2.1) — the gating manual step; do not skip or shortcut.
4. Build `bc_phase0_benchmark.py`; add the approach-3 sidecar enumerator (drugsfda/FedReg/AdComm reuse) and
   the approach-2 *assessment* (no scraper — ToS/cost notes + optional one-shot sample) so all three are
   scored.
5. Run the benchmark → produce the report → evaluate §4 GO/NO-GO. **Decision point with Pedro.**
6. On GO: wire the Modal cron (DB `scanners` row, hour 11 UTC) + `app.py` registration; confirm a live
   scheduled run writes `bc_pipeline_runs` and lights up `bc_candidates` (as `gate1_failed` until Phase 1).

**Reuse anchors (paths):** `modal_workers/fetchers/universe/edgar_8k_pdufa.py` (EFTS+CIK/ticker extract,
retry, idempotent write skeleton); `modal_workers/scanners/fda_pdufa_pipeline.py` (`_discover_pdufa_from_edgar`,
`_parse_filing_for_pdufa` @524); `modal_workers/shared/edgar_efts.py` (`efts_search`, `fetch_filing_text`);
`modal_workers/providers/polygon/{base,market_data,options_data}.py` (tradeability); `modal_workers/shared/
sponsor_resolver.py` + `entity_identifiers` (CIK/ticker); `modal_workers/shared/openfda_client.py` +
`modal_workers/ingestion/openfda_ingest.py` (`ingest_drugsfda_approvals`, `extract_submission_rows`) and
`modal_workers/fetchers/universe/{fda_adcomm_pdufa,fed_register_adcom}.py` (approach-3 sidecar);
`modal_workers/shared/supabase_client.py` (`_rest_with_retry`, ON CONFLICT); `modal_workers/app.py`
(`dispatch_release_times` registry cron pattern); `modal_workers/shared/fda_crl/feature_assembly.py`
(designation/feature semantics the Phase-1 scorer expects — keep `bc_application_features` compatible).
