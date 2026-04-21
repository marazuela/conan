# CONTEXT — Litigation & Docket Signal System (Tool 3)

> Only read this if INSTRUCTIONS.md doesn't have the detail you need.

---

## Why This Domain Has an Asymmetry

The legal system is **simultaneously public and obscure**. Every filing in federal court is docketed in a search-indexed database; every ITC investigation publishes a Federal Register notice; every PTAB petition has a case number and a scheduling order; every SEC enforcement action is announced the same day. The data is free, real-time, and machine-indexable in principle.

And yet equity analysts read almost none of it, because:

1. **Party names are not tickers.** A case captioned "Acme Holdings LLC v. Globex Inc." names two legal entities that may or may not be the publicly-traded parent. The parsing from legal-entity-name to issuer-FIGI is non-trivial (subsidiaries, holding companies, acquired entities, DBAs). No off-the-shelf mapper exists.
2. **Docket free-text is heterogeneous.** Every court has its own docket schema, conventions, and clerk practices. Bankruptcy dockets are a different world from civil dockets. PTAB scheduling orders use a different format from ITC institution notices.
3. **PDFs are the norm.** Most substantive filings (complaints, motions, orders) are PDFs, often scanned, often with caption pages that are formulaic but not reliably machine-readable.
4. **The interesting subset is small.** Out of ~400,000 federal civil filings per year, maybe 2,000 involve public companies and are market-material. The signal-to-noise is thin unless you filter aggressively.
5. **Coverage collapses beyond tier-1 firms.** Bloomberg Law, Law360, and Westlaw cover the top ~500 names well. Below that, coverage is spotty, delayed, and often behind a second paywall.

The edge persists because these frictions are structural, not transient. A tool that solves (1) (party resolution) at 80%+ precision and (2)–(5) at "good enough" fidelity gets first-mover information on 200–500 material filings per year that equity markets don't yet know about.

---

## Strategy Selection Rationale

The six channels were chosen to maximize signal density while keeping the build tractable:

- **PACER/RECAP (federal civil)** — the highest-volume, highest-signal docket in the US legal system. Every patent case, antitrust case, securities class action, and most commercial disputes involving public companies are here. RECAP (the free mirror from the Free Law Project) covers a meaningful subset for free.
- **ITC Section 337** — structurally underweight in equity research relative to its impact. Institution of a 337 investigation is a binary event for component makers, chip designers, pharmaceutical generics. EDIS is free and well-documented.
- **PTAB IPR** — patent-validity outcomes move small- and mid-cap biotech and tech stocks decisively. PTAB End-to-End is free, clean, and has a deterministic schedule.
- **Delaware Chancery** — the dominant court for M&A disputes, appraisal actions, and DGCL 220 books-and-records demands. Appraisal filings in announced deals are strong signals of deal-break risk. The free index is HTML-scraping-only, which is itself the moat.
- **SEC Enforcement** — litigation releases are published the day of enforcement action. Wells Notices occasionally leak through 10-Q risk factors but the enforcement docket is the first public signal.
- **DOJ/FTC Antitrust** — Second Requests on announced M&A and merger challenges filed in federal court are public days before companies 8-K them.

**Excluded from initial scope (see D-007):** federal criminal (noise-heavy, mostly not equity-relevant), state courts beyond Delaware (volume prohibitive), bankruptcy courts (schema complexity — Phase 2), international litigation (Phase 3), administrative warning letters (Tool 1 partial coverage).

---

## API & Data Source Reference — VALIDATED PHASE 1 (2026-04-14)

Endpoint validation is the first step of every phase. The table below reflects live probes from the Cowork sandbox on 2026-04-14. See `PROGRESS_LOG.md` session-2 block for raw probe results, and `DECISIONS.md` D-014 through D-016 for mitigations on blocked/degraded endpoints.

### Primary endpoints

| # | Channel | Endpoint (current) | Auth | Cost | Status |
|---|---------|-------------------|------|------|--------|
| 1 | CourtListener RECAP API | `www.courtlistener.com/api/rest/v4/` (search + docket endpoints) | API token (free, registration required) | Free for indexed content | ✅ VERIFIED 2026-04-14 (HTTP 200, full endpoint catalog returned) |
| 1 | CourtListener RECAP Archive | `www.courtlistener.com/recap/` | None | Free | ✅ VERIFIED 2026-04-14 (reachable; bulk is via `/api/rest/v4/recap/`) |
| 1 | PACER Case Locator | `pcl.uscourts.gov` | PACER account | $0.10/page — **NOT used autonomously in v1 (D-008)** | 🚫 FLAG-FOR-MANUAL (not probed; policy-locked) |
| 2 | USITC EDIS external UI | `edis.usitc.gov/external/` | None | Free | ✅ VERIFIED 2026-04-14 (HTTP 200; no Swagger — REST spec is in PDF, D-016) |
| 2 | USITC EDIS REST API | per `usitc.gov/sites/default/files/press_room/documents/edis_data_web_service_guide.pdf` | None (rate-limit key recommended) | Free | ⚠️ SPEC-BLOCKED (PDF returned 301 redirect; spec needs re-pull before Phase 3) |
| 2 | USITC news releases (press room) | `www.usitc.gov/news_releases` (redirects `/press_room/news_release` → new path; individual releases at `/press_room/news_release/YYYY/erMMDD_NNNNN.htm`) | None | Free | ✅ VERIFIED 2026-04-14 (HTTP 200; 2026 releases enumerable; **UA-SENSITIVE — see D-015**) |
| 3 | USPTO PTAB API v3 (ODP) | `data.uspto.gov/api/v1/patent/trials/proceedings/search` (Swagger: `data.uspto.gov/swagger/index.html`) | API key per ODP policy | Free | ⚠️ WAF-GATED — HTTP 200 to the Angular shell but API calls challenged by AWS WAF (`0dd6fc7fe1e2.edge.sdk.awswaf.com`). See D-014 mitigation. |
| 3 | USPTO PTAB API v2 (Developer Hub) | `developer.uspto.gov/ptab-api/` | API key | Free | ⛔ DECOMMISSIONING 2026-04-20 per USPTO global banner. **Do NOT build against v2.** (D-014) |
| 3 | PTAB search portal (HTML) | `data.uspto.gov/ptab` (Angular SPA) | None | Free | ⚠️ WAF-GATED (same mitigation path as PTAB v3 API) |
| 4 | Delaware CourtConnect (Avenu "Contexte") | `courtconnect.courts.delaware.gov/cc/cconnect/ck_public_qry_main.cp_main_idx` | None | Free (HTML scraping, frameset) | ✅ VERIFIED 2026-04-14 — **NO CAPTCHA** (resolves Q-002 negative). Disclaimer interstitial only. Tokenless two-hop flow: `cp_main_idx` → `cp_main_disclaimer` → `cp_disclaimer_srch_link` → search form. |
| 4 | Delaware Chancery opinions index | `courts.delaware.gov/opinions/` (scrape; `opinions/index.aspx?ag=court%20of%20chancery`) | None | Free | ✅ VERIFIED 2026-04-14 (HTTP 200). **Note**: `chancery/rss.aspx` returns 200 but body is a "Page Not Found" HTML — **no RSS feed exists** (original plan-note was wrong). Chancery reading is opinions-scrape + CourtConnect docket-search, not RSS. |
| 5 | SEC EDGAR getcurrent feed | `www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=&company=&dateb=&owner=include&count=40&output=atom` | SEC-compliant UA header | Free | ✅ VERIFIED 2026-04-14 (HTTP 200, atom feed). Form-filtered variant (`type=8-K`) also 200 after UA attached. |
| 5 | SEC EDGAR full-text search | `efts.sec.gov/LATEST/search-index?q=...&forms=...` | SEC-compliant UA | Free | ✅ VERIFIED 2026-04-14 (HTTP 200 on litigation and Exhibit-21 queries) |
| 5 | SEC Litigation Releases (landing) | `www.sec.gov/litigation/litreleases` (301 from `.htm`) | SEC-compliant UA | Free | ✅ VERIFIED 2026-04-14 (HTTP 301 → reachable landing page) |
| 5 | SEC Enforcement press releases | `www.sec.gov/news/pressrelease` | SEC-compliant UA | Free | ⚠️ NOT DIRECTLY PROBED — covered indirectly via EDGAR full-text + getcurrent. Phase 3 will resolve exact path. |
| 6 | DOJ Antitrust press releases | `www.justice.gov/atr/press-releases` | None | Free | ✅ VERIFIED 2026-04-14 (HTTP 200). No ATR-specific RSS: `/feed/press_releases/rss.xml` → 404, `/rss/atr` → 404. Global news RSS at `/news/rss` is HTTP 200. Scanner must scrape `/atr/press-releases` or filter the global RSS. |
| 6 | FTC press releases (competition) | `www.ftc.gov/feeds/press-release-competition.xml` | None | Free | ✅ VERIFIED 2026-04-14 (HTTP 200, RSS). Also: `/feeds/press-release.xml` (all), `/news-events/news/press-releases` HTML landing — both 200. |
| 6 | FTC Cases and Proceedings | `www.ftc.gov/legal-library/browse/cases-proceedings` | None | Free | ⚠️ NOT PROBED THIS PASS — landing page confirmed reachable via its parent paths. Phase 3 enumerates. |

### Support endpoints (re-verified from Tool 1)

| Source | Endpoint | Auth | Status |
|--------|----------|------|--------|
| OpenFIGI v3 | `api.openfigi.com/v3/mapping` | None | ✅ RE-VERIFIED 2026-04-14 (HTTP 200 on POST with well-formed body; HTTP 405 on GET, as expected — endpoint is POST-only) |
| SEC data.sec.gov | `data.sec.gov/submissions/CIK{CIK}.json` | User-Agent (email) | ✅ RE-VERIFIED 2026-04-14 (HTTP 200 on CIK0000320193 sample) |
| Yahoo Finance (yfinance library) | Python `yfinance` package | None | ⏭️ DEFERRED — Python probe not run this phase. Re-verify in Phase 3 before first scanner run. |

### Party-resolution lookup sources

| Source | Use | Cost | Status |
|--------|-----|------|--------|
| SEC EDGAR full-text company search | Legal-entity name → CIK → ticker | Free | ✅ VERIFIED 2026-04-14 (via `efts.sec.gov/LATEST/search-index`) |
| SEC 10-K Exhibit 21 corpus | Subsidiary name → parent CIK (authoritative) | Free | ✅ VERIFIED 2026-04-14 (full-text query `"Exhibit+21"&forms=10-K` returns results; corpus access confirmed; extraction pipeline is Phase 2 build) |
| OpenFIGI mapping by `NAME` idType | Legal-entity name → FIGI (low precision — fallback only) | Free | ✅ RE-VERIFIED 2026-04-14 (POST endpoint live) |
| Wikidata entity search + SPARQL | Legal-entity → parent company relations | Free | ✅ VERIFIED 2026-04-14 (`wikidata.org/w/api.php` HTTP 200; `query.wikidata.org/sparql` HTTP 200) |

### Verification discipline note

Per template Part 2 ("verify don't remember"), Status column entries carry the probe date. A ✅ VERIFIED entry that is more than 90 days old at the start of a future phase must be re-probed before being relied on. D-014, D-015, D-016 capture the non-trivial mitigations needed downstream.

---

## Entity Resolution Protocol — The Litigation-Specific Challenge

Tools 1 and 2 resolve entities from `ticker + MIC` → `FIGI`. Litigation can't do that because court captions use legal-entity names, not tickers. The new protocol is **two-stage**:

### Stage 1 — Party Name Normalization

Every signal starts with a raw party string from the docket (e.g., "Acme Holdings LLC", "Globex Inc.", "John Q. Smith, an individual").

1. Strip corporate-form suffixes (`LLC`, `Inc.`, `Corp.`, `Ltd.`, `PLC`, `L.P.`, `LLP`, `GmbH`, `N.V.`, `S.A.`).
2. Normalize whitespace, punctuation, case.
3. Classify: `corporate_entity` | `individual` | `government` | `unknown`. Only `corporate_entity` proceeds to Stage 2.
4. Flag any caption party that is an individual who may be a public-company executive (for SEC enforcement scanner — executive actions are a valuable sub-signal; these are resolved separately via a lightweight executive lookup table).

### Stage 2 — Entity Resolution to Issuer FIGI

Tried in order, first success wins. Confidence score recorded on every attempt.

1. **Exact match in internal party→issuer cache** (built up across sessions; starts empty, grows monotonically). Confidence: 1.0.
2. **SEC EDGAR company-name search** — `efts.sec.gov/LATEST/search-index` with `forms=10-K&q="<name>"`, returns CIK if match. Cross-walk CIK → ticker → OpenFIGI. Confidence: 0.95 on exact match, 0.80 on fuzzy match (normalized Levenshtein ≤ 3).
3. **10-K Exhibit 21 subsidiary lookup** — pre-built table (updated quarterly) mapping subsidiary names to parent CIKs from the entire 10-K universe's Exhibit 21 filings. Confidence: 0.90 for direct subsidiary, 0.75 for indirect (referenced via intermediate holding).
4. **OpenFIGI `NAME` idType mapping** — last resort, low precision, many false positives. Confidence: ≤ 0.70. Signals resolving only at this stage are triaged out at Stage 1 of the signal pipeline (see D-003).
5. **Unresolved** — logged to `working/unresolved_parties.md` with party string, case reference, and reason. Reviewed in maintenance sessions; resolved entries augment the internal cache.

### Confidence Thresholds for Signal Admission

- Confidence ≥ 0.85 — signal admitted to convergence engine and scoring.
- 0.70 ≤ Confidence < 0.85 — signal admitted but `resolution_confidence` caveat recorded; triaged at Stage 1 unless other channels corroborate the entity.
- Confidence < 0.70 — signal logged but excluded from active pipeline.

**Never key convergence on a legal-entity name string.** Convergence must key on `issuer_figi`. This mirrors Tool 2's D-004 principle for cross-listing and is the single most important correctness rule in the entity system.

---

## Signal JSON Schema (extends Tool 1/2 schema)

```json
{
  "entity_id": "0000320193",
  "entity_aux_id": "AAPL",
  "entity_name": "Apple Inc.",
  "entity_size_metric": 3000000000000,
  "signal_type": "motion_to_dismiss_denied",
  "signal_category": "federal_civil",
  "strength_estimate": 4.2,
  "source_url": "https://www.courtlistener.com/docket/...",
  "source_date": "2026-04-12",
  "scan_date": "2026-04-14T10:00:00Z",
  "raw_data": {
    "court": "N.D. Cal.",
    "case_number": "3:25-cv-01234",
    "case_caption": "Foo Corp. v. Apple Inc.",
    "docket_entry": "Order denying motion to dismiss",
    "party_role": "defendant",
    "party_raw_name": "Apple Inc.",
    "resolution_method": "sec_edgar_exact",
    "resolution_confidence": 0.95
  }
}
```

The new fields relative to Tool 1/2 are inside `raw_data`: `court`, `case_number`, `case_caption`, `party_role`, `party_raw_name`, `resolution_method`, `resolution_confidence`. Keeping them inside `raw_data` preserves the outer schema unchanged — which is what lets cross-tool convergence operate on a single shape.

---

## Scoring Quick Reference

**7 dimensions**: Signal Strength (×2), Catalyst Clarity (×1.5 — elevated from Tool 1 because legal calendars are deterministic), Info Asymmetry (×1.5), Risk/Reward (×1), Edge Decay (×1), Liquidity (×1), Party-Resolution Confidence (×1 — **NEW**, replaces Catalyst Timeline).

Max: 42.5 | Convergence bonus: +4 (2 channels), +8 (3+ channels) | **28+ Immediate, 22–27 Watch, 14–21 Archive, <14 Discard**

Full rubric: `framework/scoring_system.md`.

---

## Convergence Window

30-day rolling window, wider than Tool 1/2's 14-day window. Rationale: litigation events on the same entity often span weeks (complaint filed Day 0 → service Day 5 → motion to dismiss Day 30 → hearing Day 60). A 14-day window would miss these chains. See D-005.

---

## Cadence Rationale

| Channel | Cadence | Why |
|---------|---------|-----|
| Federal Civil (PACER/RECAP) | Every 6h | Highest-volume channel; RECAP updates hourly for many courts |
| ITC 337 | Every 12h | Institution notices are weekly at most; daily sufficient but 12h catches same-day notices |
| PTAB IPR | Daily | PTAB updates mid-day ET; once-daily post-update is sufficient |
| Delaware Chancery | Every 12h | Chancery opinions posted irregularly; 12h is the minimum viable |
| SEC Enforcement | Every 6h | SEC litigation releases published intraday; 6h captures same-day |
| DOJ/FTC Antitrust | Every 12h | Press releases daily at most |

The 3-hourly cadence of Tool 1 is NOT carried over. Litigation moves slower than financial disclosures. See D-005.

---

## Execution Environment

```bash
pip install requests beautifulsoup4 lxml yfinance openpyxl pandas pypdf rapidfuzz reportlab python-docx --break-system-packages
```

New dependencies relative to Tool 1:
- `pypdf` — reading PDF complaints, orders, exhibits (free, headline-only for v1; full-text extraction in Phase 2+).
- `rapidfuzz` — fast Levenshtein for party-name fuzzy matching in the resolution stage.
- `reportlab` — direct PDF generation for the performance report (never docx→pdf chain; template Part 5.3).
- `python-docx` — litigation-brief docx deliverables.

Path-mapping discipline from PROJECT_TEMPLATE Part 13 applies unchanged.
