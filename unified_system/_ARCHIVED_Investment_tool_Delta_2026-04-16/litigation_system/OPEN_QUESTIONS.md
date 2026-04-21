# OPEN QUESTIONS — Litigation & Docket Signal System (Tool 3)

Numbered sequentially, `Q-001`, `Q-002`, … Scheduled sessions NEVER ask questions in chat — they append here and continue with unblocked work. Interactive sessions may also append rather than block if the question doesn't gate progress.

Format for each question:

```
## Q-NNN — <short title>
Status: OPEN | ANSWERED (date)
Raised: YYYY-MM-DD Session N
Question: <precise question>
Context: <why it matters>
What we'd need to resolve it: <next step or information required>
Current workaround: <if any>
```

A question moves to `Status: ANSWERED` by editing its header line only (the question body stays intact) and adding an `Answer:` block beneath, plus a reference to the decision (`D-0XX`) that codifies the resolution if any.

---

## Q-001 — Judge-effect modeling on Signal Strength scoring (F-19)

Status: OPEN
Raised: 2026-04-14 Session 1 (instantiation)
Question: Motion-to-dismiss grant rates vary up to 3× by federal judge (some grant 60%+, others grant 15%). The Signal Strength scoring dimension currently assumes population averages. Should the rubric factor in a per-judge prior when a docket entry is tied to a specific judge?
Context: Without judge-effect modeling, signals from hostile-judge districts are over-scored (a motion-to-dismiss denial is less remarkable before a pro-plaintiff judge) and signals from pro-defense judges are under-scored. F-19 flags this as accepted scoring-precision limitation for v1.
What we'd need to resolve it: A dataset of ≥ 6 months of scan data showing per-judge grant rates. D-003's scoring rubric weight structure allows the decision to be "add a judge-prior multiplier to Signal Strength" without changing Scoring dimensions themselves.
Current workaround: Use population averages in v1. Revisit as a Phase 8+ candidate scope per PHASING file.

---

## Q-002 — Delaware Chancery CAPTCHA / session-management feasibility from Cowork sandbox

Status: ANSWERED 2026-04-14 (Phase 1, Session 2)
Raised: 2026-04-14 Session 1 (instantiation)
Question: Does the Delaware Courts public docket search (`courts.delaware.gov/help/onlineservices/docketsearch.aspx`) require session cookies or CAPTCHA responses that the Cowork sandbox cannot produce? If so, Chancery scanner falls back to RSS-only coverage.
Context: LITIGATION_STRATEGIES.md notes Chancery's search is "HTML-only, slow, and requires CAPTCHA-adjacent session management to search fully." Live-probing from the sandbox is the only way to know whether basic docket search is reachable.
What we'd need to resolve it: Phase 1 endpoint validation will probe `courts.delaware.gov`. If reachable with plain HTTP + session cookies, scanner builds as specified. If CAPTCHA gate is hit, scanner degrades to RSS-only mode and a new decision (D-0XX) documents the reduced scope.
Current workaround: Design the Chancery scanner with RSS as the primary path and docket-search as enrichment (matches F-11 mitigation). Fallback is graceful.

**Answer**: Delaware's public docket is served through **CourtConnect** (Avenu "Contexte" product) at `courtconnect.courts.delaware.gov/cc/cconnect/`, NOT at `courts.delaware.gov/help/onlineservices/docketsearch.aspx` (that URL is stale). The entry point is `ck_public_qry_main.cp_main_idx` which renders as an HTML frameset with three options (search by party name / judge / docket). Hitting `cp_main_disclaimer?search_option=party` returns a disclaimer interstitial that points to the actual search form — **no CAPTCHA, no reCAPTCHA, no h-captcha, no JS challenge**. Content-Security-Policy is present but does not gate scraping.

Additionally, `courts.delaware.gov/chancery/rss.aspx` returns HTTP 200 but the body is a "Page Not Found" HTML page — **the chancery RSS endpoint in the original plan does not exist**. The real Chancery scrape surface is `courts.delaware.gov/opinions/index.aspx?ag=court%20of%20chancery` (opinions only) plus CourtConnect for new-filing discovery.

Implications:
- The original "RSS primary, docket-search enrichment" design inverts: docket-search (CourtConnect) is the only reliable new-filings surface; opinions-scrape is enrichment for decided matters.
- Scanner must parse the frameset (three nested frames per page) and follow the disclaimer → search flow on each session.
- D-016 captures the revised Chancery scanner design.
- `strategies/strategy_delaware_chancery.md` must be rewritten in Phase 3 to match.

---

## Q-003 — CourtListener free-API-tier rate limit sufficiency at steady state

Status: PARTIALLY ANSWERED 2026-04-14 (Phase 1, Session 2) — remains OPEN pending steady-state load test
Raised: 2026-04-14 Session 1 (instantiation)
Question: The CourtListener RECAP API has documented 5000 req/hour for registered free-tier users. Is that budget enough for Federal Civil scanner (every 6h) plus DOJ/FTC scanner's PACER cross-reference (every 12h), at the target ~400k universe × a conservative filter?
Context: If rate limits are insufficient, the scanner needs to sub-query (narrowing nature-of-suit codes per pass) or the cadence tightens. Either way it is a Phase 1 constraint, not a Phase 2 discovery.
What we'd need to resolve it: Phase 1 live-probe; log 429 responses into `SESSION_STATE.md` Tool Health. If sustained rate-limit issues appear, open a D-0XX to document sub-query strategy.
Current workaround: Per-scanner budget of 500 requests per pass (well under 5000/hr). Maintenance task monitors for sustained 429s per F-16.

**Partial answer**: CourtListener API v4 root (`/api/rest/v4/`) responded HTTP 200 with the full endpoint catalog on an unauthenticated probe — base URL is live and the v4 contract is stable. **But** rate-limit sufficiency can only be confirmed under real scan load (Phase 3). Budget remains provisional at 500 req/pass; observability (`scan_results/health.json`) must log per-run consumed quota and 429 count, and the maintenance task (per-F-16) raises an alert at ≥ 5% sustained 429 rate over three consecutive runs. No decision yet — will close this question once Phase 3 scanner has logged one full week of steady-state traffic.

---

## Q-004 — USPTO PTAB API v3 WAF-challenge bypass feasibility

Status: OPEN
Raised: 2026-04-14 Session 2 (Phase 1 endpoint validation)
Question: Can server-side scans from Cowork-class sandboxes successfully complete the AWS WAF challenge (`0dd6fc7fe1e2.edge.sdk.awswaf.com/.../challenge.js`) on `data.uspto.gov`, or does the PTAB v3 API effectively require either (a) a browser-context runner (e.g. Playwright/Chromium with JS execution) or (b) an official API key that exempts the caller from the browser challenge?
Context: The PTAB v3 Swagger is published, but every probe to `/api/v1/patent/trials/proceedings/search` returns the Angular SPA shell instead of JSON — the WAF serves the HTML challenge to non-browser clients. Developer Hub v2 decommissions 2026-04-20; we have ~6 days before v2 is unavailable. If v3 is unreachable without browser context, the PTAB scanner becomes a Claude-in-Chrome task or requires an API-key pathway that exempts the caller.
What we'd need to resolve it: (1) Check whether ODP offers an API-key header that bypasses the WAF challenge (`data.uspto.gov/support/` docs review). (2) If no, prototype a Playwright-based fetcher for Phase 3 and accept the latency/cost. (3) Fallback: scrape the legacy bulk PTAB data dumps at `dh-opendata.s3.amazonaws.com/` for back-archive, and use the Developer-Hub v2 API for the final 6 days to cache a snapshot before decommission.
Current workaround: Mark the channel degraded. Federal-civil, ITC, SEC, Delaware, DOJ/FTC scanners are unaffected. PTAB channel cadence drops to manual-refresh until D-014 resolves.

---

## Q-005 — USITC EDIS REST spec currency (2026)

Status: OPEN
Raised: 2026-04-14 Session 2 (Phase 1 endpoint validation)
Question: Is the EDIS REST API spec still the PDF at `usitc.gov/sites/default/files/press_room/documents/edis_data_web_service_guide.pdf`, and is it still accurate? The PDF link returned HTTP 301 on the live probe (a redirect, not the file) — the canonical location may have moved.
Context: EDIS is the authoritative source for ITC Section 337 investigation filings. The EDIS UI is confirmed reachable at `edis.usitc.gov/external/`, but without the current REST spec we cannot build a reliable ITC scanner in Phase 3. The PDF was referenced directly in the EDIS external page's "API" button, so the link exists — just redirected on our probe (likely `.pdf → newer path`).
What we'd need to resolve it: Follow the 301 redirect (curl `-L` or check `Location` header) and pull the actual spec file. Validate that the documented REST base URL and endpoints (investigation search, document search, full-text) are still live. If the URL or JSON shape has changed materially, open a decision.
Current workaround: Scanner path for Phase 3 assumes EDIS UI scrape as primary and REST API as enrichment once Q-005 closes. Press-room scrape of `/press_room/news_release/YYYY/erMMDD_NNNNN.htm` remains a reliable ITC coverage surface independent of EDIS.
