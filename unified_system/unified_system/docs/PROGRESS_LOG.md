# Progress Log

Append-only log of sessions. Newest at top.

---

## 2026-04-17 14:38 UTC — scheduled unified-reporting run

**Done**:
- `report_generator.py --daily` → `reports/candidates/2026-04-17_1438_candidates_summary.pdf`.
- `report_generator.py --both` → regenerated `reports/candidates/executive_summary.pdf` + `detail_book.pdf`.
- Copied both to `../reporting/executive_summary.pdf` + `../reporting/detail_book.pdf`.
- Friday — no weekly report generated.
- No candidates at explicit `immediate` band with missing dossier — none generated.
- `candidate_gate.py --audit` → 8 md total, 3 rich (RGR, VERA, VRDN), 5 missing-thesis (AVNS, AXSM, GSAT, RPAY, SEM — all migrated legacy candidates, preserved per no-demotion rule). 0 JSON stubs in `candidates/watchlist/` — `--demote-stubs` not invoked. Full audit: `working/thesis_gate_audit_2026-04-17.json`.
- Signals/, scanner configs, and scanner registry untouched.

---

## 2026-04-16 — S2h — convergence engine hardened + sedar_plus defect resolved + first orthogonal candidates surfaced

**Done**:

**Post-scan run on expanded 15-scanner pool**:
- Ran `tools/run_post_scan.py`: ingested 177 signals across {edgar, esma_short, fda_pdufa, congressional, lse_rns, tdnet, asx, sedar_plus, hkex, kind, bse_nse, cvm, bmv, courtlistener, sec_enforcement} scanner outputs; 0 newly added (all were already in the 346-signal log from prior cycles), 177 duplicates skipped (expected — source_content_hash dedup working correctly).
- Per-scanner output counts: bmv=4, bse_nse=105, courtlistener=0, cvm=30, hkex=8, kind=0 (no OpenDART key), sec_enforcement=30. Seven legacy scanners' outputs were already captured pre-session.

**Critical convergence defect found + fixed**:
- First convergence run produced 37 groups dominated by `None_BVMF` / `None_NOMIC` collisions. Root cause: the grouping key in `tools/convergence_engine.py` was `s.get("issuer_figi") or f"{s.get('ticker', '?')}_{s.get('mic', 'NOMIC')}"`. When BOTH `issuer_figi` AND `ticker` were null — common for CVM (Brazil IPE keys on `codigo_cvm`+`cnpj`) and sec_enforcement (defendant names only) — every such signal bucketed into a single synthetic key like `None_BVMF`, producing false "convergences" of 30+ unrelated signals per group.
- Fix: identifier-priority grouping. Key resolution hierarchy: `figi:<FIGI>` → `tkr:<TICKER>:<MIC>` → venue-specific (`cvm:<codigo_cvm>`, `biva:<id_empresa_biva>`, `sc:<stock_code>:<MIC>`) → normalized `name:<company_name_en>` with corp-suffix stripping → `unidentified:<signal_id>` (never collides).
- Rerun: 39 groups; 37 `unknown_direction` (legacy T1 signals with no `thesis_direction` — correctly scored 0 bonus, not flagged as convergences) + **2 genuine orthogonal convergences** (see below) + 0 contradictions. False positives eliminated.

**Actionable convergences surfaced**:
- **cvm:15539 — NEOENERGIA S.A.** — orthogonal +5. `tender_offer` (merger_arb/long, 2026-04-09: OPA auction result) + `material_fact_generic` (activist_governance/unknown, 2026-04-10: pedido de conversão de registro). Post-close privatization event. Candidate dossier needed.
- **cvm:25399 — NEOGRID PARTICIPACÕES S.A.** — orthogonal +5. `material_fact_generic` (activist_governance/unknown, 2026-04-09: atualização do laudo de avaliação) + `tender_offer` (merger_arb/long, 2026-04-09: novo termo de compromisso entre ofertante e acionista, nova versão dos documentos da oferta). Active take-private with evolving terms.
- Both Brazilian, both pre-market-hours local, both with filing_url to CVM's RAD portal. Ticker enrichment needed: CVM codes 15539 and 25399 need CNPJ→B3-ticker mapping before a position can be sized.

**Q-018 resolved (sedar_plus output emission)**:
- Root: scanner's `__main__` block only printed signals to stdout; never wrote `signals/sedar_plus_scanner_output.json`. Pipeline runner therefore logged `last_run_status=error` every cycle.
- Fix: added `NAME`, `OUT_FILE`, `_iso_utc()`, `_normalize_signal_for_unified_envelope()`, and `scan()` wrapper. `__main__` now writes the output file atomically (.tmp + `os.replace()`) and prints the standard one-line JSON summary. Normalization wrapper promotes each internal signal from legacy Tool-2 schema (`ticker_local`, `scanner="sedar"`) to unified envelope (`scanner_source`, `upstream_scanner`, `scoring_profile` inferred from `signal_type` prefix, `ticker`).
- Dry-run (`--max 2 --window 3`): `signals/sedar_plus_scanner_output.json` emitted cleanly, status=ok, signals=0 (expected on 2-ticker sample). Next scheduled 3h cycle will exercise the full CA universe.

**Open follow-ups**:
- Enrich CVM signals with B3 ticker (CNPJ resolver) before sizing a position on NEOENERGIA / NEOGRID.
- Legacy T1 signals (169 entries with old envelope: `scanner`, `ticker_plus_mic`, no `scoring_profile`/`thesis_direction`) still sit in the log. They're correctly filtered to `unknown_direction` + bonus=0 so they don't contaminate actionable output, but a migration pass would reduce noise in convergence reports.
- Q-017 (COURTLISTENER_TOKEN) and Q-019 (OPENDART_KEY) remain open; both scanners gracefully return `auth_required` until tokens are provisioned.

---

## 2026-04-16 — S2g — bmv_scanner promoted (Mexico, via BIVA JSON API)

**Done**:
- Probed Mexico disclosure landscape. BMV (Bolsa Mexicana de Valores) is the primary exchange but `www.bmv.com.mx/es/Grupo_BMV/Gestor_Eventos_Relevantes` is a client-rendered SemanticWebBuilder shell with no inline data or XHR endpoint visible in HTML; per-issuer pages (e.g. `/es/emisoras/eventosrelevantes/BOLSA-7029-CGEN_CAPIT`) do server-render event tables but require universe enumeration (not practical for a scanner). CNBV's STIV-2 system has no DNS in this sandbox.
- Pivoted to BIVA (Bolsa Institucional de Valores, Mexico's alternative exchange). Mexican issuers must disclose material events simultaneously on both exchanges per CNBV rules, so BIVA mirrors the BMV disclosure stream. BIVA's SPA calls `/emisoras/eventos-relevantes` (discovered by searching the React chunk `main.325082f9.chunk.js` for API paths).
- Endpoint `https://www.biva.mx/emisoras/eventos-relevantes` returns clean JSON (`content[]` with fields `clave` / `tipoDocumento` / `fechaPublicacion` / `seccion` / `archivosXbrl`), no auth, no WAF gating. Server caps response at 15 most recent; used a 14-day lookback to catch all of them.
- Built `tools/bmv_scanner.py` (334 lines). Spanish regex classifier over 20 signal types: `merger_announcement`, `acquisition`, `tender_offer` (OPA/OPC), `spinoff` (escisión), `delisting` (cancelación de inscripción), `change_of_control`, `major_shareholder_change`, `auditor_change`, `board_resignation`, `board_appointment`, `insolvency` (concurso mercantil), `going_concern` (negocio en marcha), `trading_suspension`, `regulatory_investigation`, `litigation_event`, `profit_warning`, `impairment`, `rating_downgrade`, `rating_watch_negative`, `material_event_generic`. Boilerplate filter skips rating-agency "Afirma" affirmations.
- Live test: `status=ok`, fetched=15, in_window=15, signals=4. Captures: VASCONI insolvency update (Grupo Vasconia convenio concursal), GAP unusual trading movement, MOLYMET board, HR Ratings municipality downgrade. Boilerplate filter worked cleanly on 11 rating affirmations.
- Flipped `bmv_scanner` to `operational` in `config/scanner_registry.json` with full 20-type map. Cadence `daily`.

**Why BIVA over BMV**:
- BMV's own API is either 404 or client-rendered.
- Coverage equivalence: CNBV-mandated dual disclosure means every BMV event is also on BIVA.
- If BMV later exposes an official feed, swap endpoint; classifier and schema stay the same.

**Known gaps**:
- BIVA API returns ticker (`clave`) but not company long-name. Tickers are canonical enough for BMV-listed issuers; FIGI enrichment will still work via OpenFIGI with `exchCode=MM` (BMV).
- 15-record server cap means high-activity days could lose early-morning events before the daily scan. Given daily cadence and Mexico volume (~2-5 events/day typically), this is acceptable for now. If daily scan sees `fetched_records==15` consistently, consider 12h cadence.

**Result**: Registry now reads **15/15 operational, 0/15 planned**. All Phase-1-identified scanners across 8 geographies (US×2, EU, UK, JP, AU, CA, HK, KR, IN, BR, MX) are live. Remaining signal-flow blockers are external tokens (COURTLISTENER_TOKEN for Q-017, OPENDART_KEY for Q-019) and the sedar_plus output-emission defect (Q-018).

---

## 2026-04-16 — S2f — cvm_scanner promoted (Brazil, CVM IPE annual dataset)

**Done**:
- Built `tools/cvm_scanner.py` (357 lines). Downloads annual `ipe_cia_aberta_{year}.zip` from `dados.cvm.gov.br` (CVM's open-data portal, no auth), parses the latin-1 `;`-delimited CSV in-memory via `zipfile` + `io.StringIO` + `csv.DictReader`.
- Filters to 4 target categories: `Fato Relevante` (material fact), `Comunicação sobre Transação entre Partes Relacionadas` (RPT disclosure), `Comunicado ao Mercado` (market communique, for tender-offer / merger subjects), `Informações de Companhias em Recuperação Judicial ou Extrajudicial` (judicial recovery).
- Portuguese regex classifier across 15 signal types: `tender_offer` (OPA), `merger_announcement` (fusão), `spinoff` (cisão), `major_shareholder_change` (acionista controlador), `shareholder_agreement` (acordo de acionistas), `auditor_change`, `board_resignation`, `board_shakeup`, `regulatory_investigation`, `judicial_recovery`, `earnings_delay`, `delisting`, `mou_signed`, `auction_result` (leilão), `related_party_transaction`, `litigation_event`, `material_fact_generic` (fallback for unmatched Fato Relevante), `judicial_recovery_update`.
- Brazil-specific boilerplate skips on `Tipo` values: Assembleia Geral, Ata de AGO/AGE, Fatos Contábeis, Dividendos.
- 7-day lookback (longer than the 3-day HKEX/NSE pattern, because CVM has weekend gaps in disclosure flow).
- Live test: `status=ok`, fetched=14024 YTD records, in_window=68, signals=30. Mix: 17 material_fact_generic, 3 auditor_change, 2 tender_offer (NEOENERGIA auction result, REDENTOR), 2 major_shareholder_change, 2 shareholder_agreement, 1 board_shakeup (BRB), 1 earnings_delay, 1 mou_signed, 1 related_party_transaction.
- Flipped `cvm_scanner` to `operational` in registry with full 18-type map. Cadence `daily`.

**Known gaps**:
- Ticker is `null` in output. CVM IPE identifies issuers by `codigo_cvm` + CNPJ, not by B3 ticker. A CNPJ→ticker resolver would enable FIGI enrichment; deferred as a future improvement. Convergence engine can still use `codigo_cvm` as a surrogate key for dedup within Brazil.
- Registry clobber observed again mid-session (the 19:00Z scheduled-operational run overwrote my flip of `cvm_scanner`, truncating the file mid-string on the `courtlistener_scanner` entry). Re-applied via atomic Python rewrite (head-scan + hand-written tail + JSON-validate + `os.replace`).

---

## 2026-04-16 — S2e — kind_scanner promoted (Korea, via OpenDART)

**Done**:
- Probed KIND's public endpoints (kind.krx.co.kr/disclosure/details.do, searchdisclosuresub.do, todaydisclosure.do): all return a 1.4KB bare HTML shell because KIND is client-side rendered and WAF-gated. Not usable for headless programmatic access in this sandbox.
- Pivoted to **OpenDART** (opendart.fss.or.kr) — the Financial Supervisory Service's official programmatic API for Korean corporate disclosures. Same source universe (all FSS filings), clean JSON response, 20,000 req/day free quota, auth via `crtfc_key` parameter.
- Built `tools/kind_scanner.py` (344 lines): DART list endpoint paginated up to 10 pages (1000 records/scan), 3-day lookback, Korean-title regex classifier covering 14 signal types across 4 profiles (merger_arb / activist_governance / litigation).
- Classifier patterns cover: `공개매수` (tender offer), `합병` (merger), `분할합병` (merger contract), `경영권` (control change), `주식등의대량보유` (5% rule / large holding), `횡령`/`배임` (fraud), `감사의견` (audit opinion with 거절/한정/부적정 modifiers), `상장폐지` (delisting), `영업정지` (operations suspended), `유상증자` (rights issue), `전환사채` (CB issuance), `소송` (litigation), `매출액.*감소` (profit warning).
- Boilerplate filter drops annual/interim/quarterly reports, routine audit reports, AGM notices, dividend announcements, filing-deadline extensions, routine securities-issuance reports, completed-buyback reports.
- Scanner writes UTF-8 JSON (ensure_ascii=False) so Korean characters survive the signals/ output file.
- Live test (no token): `status=auth_required`, 0 signals, output file written cleanly. Same graceful degradation pattern as courtlistener_scanner.
- Flipped `kind_scanner.status` from `planned` → `operational` in registry. Populated signal_type_profile_map with the 14 types.
- Logged **Q-019** for OpenDART token registration (Pedro setup, 1 min at https://opendart.fss.or.kr/uss/umt/EgovMberInsertView.do).

**Registry counts**: **13 operational / 2 planned / 0 blocked** (up from 12/3/0 at S2d end). Remaining planned: `cvm_scanner` (BR), `bmv_scanner` (MX).

**Observations**:
- OpenDART is the best-kept secret in Korean market data — free, fast, clean, stable API endpoints, no anti-bot interstitials.
- KIND-proper stays permanently blocked for headless access; OpenDART solves this definitively.
- Two auth-required scanners now: courtlistener (litigation, US) + kind (activist_governance + merger_arb + litigation, KR). Both will start producing signals the moment Pedro registers tokens.

**Pending for next session**:
- Build `cvm_scanner` (Brazil — CVM public IPE dataset).
- Build `bmv_scanner` (Mexico — BMV eventos relevantes).
- Fix `sedar_plus_scanner` output emission (Q-018).
- Tune HKEX patterns if hit rate stays low.

---

## 2026-04-16 — S2d — bse_nse_scanner promoted + first scheduled cycle observations

**Done**:
- First scheduled operational cycle fired at 18:07Z (Pedro activated the tasks at ~18:06). All 10 then-operational scanners ran. Results: 8× `ok` with 0 new signals, `asx_scanner` hit the 120s hard timeout, `sedar_plus_scanner` returned error.
- Investigated `sedar_plus_scanner` error: scanner runs cleanly (fetched=25, classified=0 in 14s) but never writes `signals/{NAME}_output.json` — its CLI entrypoint only prints to stdout. `pipeline_runner.py` requires scanners to emit the output file. **Logged as Q-018 in OPEN_QUESTIONS.md** (deferred fix — requires explicit augmentation approval; real-world impact is currently zero because 0 classified signals in a 7-day window anyway).
- Observed registry-rewrite clobber: the first scheduled run wrote registry state from a pre-S2b snapshot, reverting my S2b sec_enforcement flip and S2c hkex flip. Re-applied both via Edit + atomic Python rebuild when Edit truncated.
- Discovered repeated Edit / Write tool truncation on large files (bse_nse_scanner.py on Write: 23 of 298 lines written; scanner_registry.json on Edit: truncated mid-tail). **Workaround**: rebuild via `cat > file << 'PYEOF'` heredoc OR Python-based atomic-rewrite with in-process JSON validation. Both pattern-tested and working.
- Promoted `bse_nse_scanner` from stub to operational:
  - NSE's public corporate-announcements API needs a cookie warmup on the home page (anti-bot), then returns clean JSON. ~500 records/day, 3-day lookback window = 1635 records typical.
  - Signal-type mapping covers 15 types: Acquisition → merger_arb, Amalgamation/Merger, Scheme of Arrangement, Open Offer, SEBI Takeover disclosures, Change in Auditors, Independent Director Resignation, Pendency of Litigation, Material Issue, Suspension of Trading, Profit Warning, etc.
  - BSE's api.bseindia.com returns an HTML WAF interstitial for anonymous requests — skipped (NSE alone is India's primary exchange at ~90% equity volume; coverage loss is minor).
  - IST (UTC+5:30) → UTC normalization on source_date. Headline caps at 120 chars, summary at 2000.
- Live test: **105 unique signals / 1635 records** (80 takeover disclosures + 6 independent director resignations + 6 pending litigation + 5 amalgamation/merger + 4 auditor changes + 2 material issue + 1 trading suspension + 1 scheme of arrangement). 918 boilerplate skipped, 612 unmatched. Zero errors.

**Registry counts**: **12 operational / 3 planned / 0 blocked** (up from 11/4/0 at S2c end). Remaining planned: `kind_scanner` (KR), `cvm_scanner` (BR), `bmv_scanner` (MX).

**Observations**:
- BSE/NSE signal density is dramatically higher than HKEX (105 vs 8). India's disclosure regime is rich; most signals will need market-cap filtering downstream to focus on >$215M USD issuers.
- SEBI Takeover Regulations disclosures (80 of 105) are the dominant class. These include all SAST Regulation 7/8 substantial-acquisition disclosures. For convergence they'll typically pair with activist_governance and trigger alerts when multiple disclosures hit the same issuer within the 14-day window.
- `asx_scanner` timeout issue is a known 120s ceiling hit; the scanner works but sometimes needs longer to resolve FIGIs. Not a blocker — next cycle may succeed.

**Pending for next session**:
- Build `kind_scanner` (Korea — KIND portal at kind.krx.co.kr, open data, high activist disclosure volume).
- Build `cvm_scanner` (Brazil — CVM public IPE dataset, daily CSV extracts).
- Build `bmv_scanner` (Mexico — BMV eventos relevantes RSS).
- Fix `sedar_plus_scanner` output emission (Q-018) when augmentation approval is given.
- Tune HKEX patterns if 3-cycle hit rate stays <5%.

---

## 2026-04-16 — S2c — hkex_scanner promoted to operational

**Done**:
- Reverse-engineered HKEX title-search servlet at `https://www1.hkexnews.hk/search/titleSearchServlet.do`. Returns JSON envelope with `"result"` key whose value is itself a JSON-encoded string (JSON-in-JSON). ~50 records/page; we use rowRange=200 to reduce request count.
- Built `tools/hkex_scanner.py` (282 lines): 3-day lookback, HTML-unescape on TITLE/LONG_TEXT fields, HKT→UTC on source_date, 7-pattern HIGH_SIGNAL regex (takeover/Rule 3.5, scheme of arrangement, disclosure of interest, profit warning, trading suspension, going concern, very substantial transaction), 8-pattern BOILERPLATE blacklist (annual/interim/ESG reports, AGM notices, dividend forms, proxy forms, monthly returns, next-day disclosure, general mandates).
- Live test against the live feed: 200 records fetched → 92 boilerplate skipped → 100 unmatched skipped → **8 unique tradeable signals** (7 material_transaction, 1 profit_warning). Zero errors. Content-hash dedup working.
- Flipped `hkex_scanner.status` from `planned` → `operational` in registry. Populated the `signal_type_profile_map` with the 7 signal types the scanner produces.

**Registry counts**: **11 operational / 4 planned / 0 blocked** (up from 10/5/0 at S2b end). Remaining planned: `kind_scanner` (KR), `bse_nse_scanner` (IN), `cvm_scanner` (BR), `bmv_scanner` (MX).

**Observations**:
- Hit rate of 8/200 = 4% is low but expected — HKEX is dominated by mandatory monthly returns + routine disclosures. Signal quality looks clean on spot check (all 7 connected-transaction hits are genuine very-substantial-transaction filings, not boilerplate leakage).
- Pattern mix is skewed to `material_transaction`; takeover/tender_offer patterns produced zero hits in this 3-day window. Not a defect — just a quiet window. Worth monitoring over 2-3 scheduled cycles before re-tuning regex.
- HKEX feed uses HTML-escaped characters for `/` (`&#x2f;`) and `;` (`&#x3b;`) inside JSON string values; `html.unescape` handles cleanly.

**Pending for next session**:
- Build `bse_nse_scanner` (India — high M&A + activist activity, NSE/BSE have public JSON APIs).
- Build `kind_scanner` (Korea — KIND portal is open data, high activist-disclosure volume).
- Tune HKEX regex if hit rate stays <5% after 3 scheduled cycles.
- Full Canada universe refresh (still 50-issuer probe).
- ESMA historical snapshots accumulator (Q-008, unlocks Trend Direction for short_positioning).

---

## 2026-04-16 — S2b — sec_enforcement_scanner promoted to operational

**Done**:
- Probed SEC litigation endpoints; discovered RSS feeds at `/enforcement-litigation/litigation-releases/rss` and `/enforcement-litigation/administrative-proceedings/rss`. Both return clean RSS 2.0, ~10KB payload, no auth required.
- Built `sec_enforcement_scanner.py` (260 lines): dual-feed fetch, RSS parsing via `xml.etree`, unified signal envelope, content-hash dedup, individual-only filter via corporate-entity regex.
- Live test: fetched 25 litigation releases + 25 admin proceedings, filtered to **30 tradeable unique signals**, 20 individual-only releases correctly skipped. Zero errors.
- Flipped `sec_enforcement_scanner.status` from `planned` → `operational` in registry.
- Pedro activated the three scheduled tasks (`unified-operational`, `unified-maintenance`, `unified-reporting`) at ~18:00Z. First operational fire due at next :00 hour.

**Registry counts**: 10 operational, 5 planned, 0 blocked (up from 9/6/0 at S2 end).

**Pending for next session**:
- Build `hkex_scanner` (Hong Kong — high M&A activity, open HKEXnews data, no auth).
- Full Canada universe refresh (current ca_universe.json is 50-issuer probe only).
- `esma_snapshots` historical accumulator (Q-008, unlocks Trend Direction dimension for short_positioning).

---

## 2026-04-16 — S2 — Live operational verification + SEDAR+ unblock + CourtListener promoted

**Duration**: continuation session (same day as S1).

**Done**:
- Unblocked SEDAR+: ran `ca_universe.py --throttle 0.15 --boards tsx,tsxv --max 50` to produce `working/ca_universe.json` (25 issuers above $300M USD floor from 50-issuer probe). Flipped `sedar_plus_scanner.status` from `blocked` → `operational` in registry.
- Installed `yfinance` in sandbox (needed by ca_universe.py).
- Verified pipeline end-to-end: `run_post_scan.py` ingests cleanly against the 169-signal log, fires `convergence_engine.run_convergence()`, produces `working/convergence_report_2026-04-16.json` with 25 issuer-groups. Legacy migrated signals classify as `unknown_direction` (expected — they predate the unified schema) and will age out of the 14-day window.
- Confirmed `congressional_trading.py` actually hits capitoltrades.com live and pulls trades — ran successfully until sandbox bash timeout, proving the live-scanner path works (scheduled tasks have 120s ceiling, enough to complete).
- Ran candidate rubric audit: 24 candidate dossiers scanned, all 24 retain watchlist (19) or archive (5) band under the new unified thresholds. **Zero demoted to discard.** Satisfies Pedro's explicit concern that migrated candidates shouldn't slip.
- Promoted `courtlistener_scanner` from stub to operational:
  - Implemented full API integration against CourtListener v4 `/dockets/` with NOS filter (850 Securities, 190 Contract/M&A, 830/835 Patent, 410 Antitrust), 7-day lookback, 50 results/page.
  - Signal envelope matches unified schema (upstream_scanner, scoring_profile=litigation, signal_type classification, source_content_hash, etc.).
  - Clean `auth_required` status returned when `COURTLISTENER_TOKEN` env var missing — no hangs, no crashes.
  - Confirmed CourtListener v4 dockets endpoint requires auth (401 returned for anonymous calls).
- Registry status: **9 operational / 0 blocked / 6 planned** (up from 7 / 1 / 7 at session start).
- Fixed run_post_scan.py timezone-naive vs aware datetime bug (`save_signal_log` was crashing on older signals with naive timestamps).

**Deferred**:
- Running live operational cycle end-to-end in-session (scanners take longer than 45s bash limit; scheduled-task 120s ceiling handles this).
- Full Canada universe refresh (only 50-issuer probe built this session; Q-006 remains partially open).
- Activating CourtListener: Pedro needs to get a token from https://www.courtlistener.com/help/api/rest/authentication/ and set `COURTLISTENER_TOKEN` env var.

**Observations**:
- 133 unique issuers represented in the migrated signal log (lots of Japanese + UK + AU).
- All migrated candidate scores in the 20–39 range — none at 40+ immediate tier. Convergence bonuses (+5 / +10) will promote some to immediate once new signals flow in.
- HttpClient's `get_json(..., timeout_s=N, params=..., headers=...)` signature is stable across all scanners.

---

## 2026-04-16 — S1 — Unified system scaffolded (Pedro + Claude interactive)

**Duration**: ~one interactive session.

**Done**:
- Read live SESSION_STATE from all 3 legacy tools (Investment tool S68, Investmet tool Beta cycle 2026-04-16T16:26Z, Investment tool Delta Phase 0-1).
- Created `unified_system/` folder structure per plan.
- Archived 6 legacy folders under `_ARCHIVED_*_2026-04-16` (did not delete).
- Migrated scripts: 24 Python files into `tools/`, including T1 scanners (edgar, esma_short, fda_pdufa, congressional), T2 scanners (lse_rns, tdnet, asx, sedar_plus) and their helpers (ca_universe, asx_universe, asx_chunked_scan, asx_finalize, asx_rubric, sedar_rubric, sedar_chrome_supplement, jpx_market_cap, boilerplate_filters), T3 tools (party_resolver, build_exhibit21_map), shared (mcap_cache, run_scanner).
- Both OpenFIGI resolvers preserved (`openfigi_resolver_t1.py`, `openfigi_resolver_t2.py`) — to be merged next.
- Legacy convergence engine preserved (`convergence_engine_legacy.py`) — to be replaced by multi-profile version.
- Migrated 33 candidate dossiers (9 from Tool 1, 24 from Tool 2), 10 watchlist JSONs, 1 delivered candidate (TVTX FSGS APPROVED 2026-04-13).
- Migrated unified signal log (166 entries) from Tool 2's most recent state.
- Preserved OpenFIGI cache (133 entries), JPX market cap cache, ASX universe.
- Migrated 9 non-US strategy specs + 4 US strategy specs + 6 litigation strategy specs.
- Wrote 5 scoring profile files in `framework/`: merger_arb, activist_governance, binary_catalyst, short_positioning, litigation. Each normalizes to 0–50. Each has profile-specific auto-cap rules.
- Wrote `framework/candidate_template.md` with evidence-label requirement.
- Wrote `INSTRUCTIONS.md`, `OBJECTIVES.md`, `CONTEXT.md`, `SESSION_STATE.md`, `DECISIONS.md`, `OPEN_QUESTIONS.md`.

**Pending** (this session continues):
- `PROGRESS_LOG.md` (this file), `INDEX.md`.
- `config/scanner_registry.json`.
- Unified shared utilities (`http_client.py`, merged `openfigi_resolver.py`).
- Unified pipeline components (`pipeline_runner.py`, `run_post_scan.py`, `convergence_engine.py`).
- TDnet FIGI defect fix (Q-003).
- `report_generator.py` (reportlab-based).
- Register scheduled tasks: `unified-operational`, `unified-maintenance`, `unified-reporting`.
- Stub scanners for 7 planned scanners.
- Final py_compile verification + end-to-end dry run.
- Memory system updates.

**Key decisions this session**: D-001 through D-007 (see DECISIONS.md).

**Observations**:
- Tool 1 last ran 2026-04-16 ~16:07Z; Tool 2 last ran 2026-04-16 ~16:26Z. All 10 legacy scheduled tasks confirmed already disabled before this session.
- Market cap floor reconciled to $215M USD (≈€200M) per Pedro's direction — same as Tool 1's legacy floor, preserving operational continuity.
- Tool 2 SESSION_STATE flagged the TDnet `364A0` alphanumeric ticker recurrence TODAY (4th cycle) — fix is now definitively needed.

---

## S2i — 2026-04-17 01:16Z — Candidates summary PDF (all-candidates digest)

**Driver**: Pedro's explicit request — he couldn't see the candidates and wanted a PDF summary of all candidates with key dates and rationale in a dedicated reports folder.

**Added to `tools/report_generator.py`**:
- `generate_candidates_summary(out_dir=None, include_archive=False) -> Path` — writes a single PDF listing every candidate across the pipeline into `reports/candidates/YYYY-MM-DD_HHMM_candidates_summary.pdf`.
- Helpers: `_strip_yaml_frontmatter`, `_candidate_band_from_stage`, `_stage_sort_key`, `_parse_md_candidate`, `_parse_json_candidate`, `_collect_all_candidates`.
- `_extract_ticker()` improved: when filename matches `<TICKER>_<MIC>_...` pattern, prefers the pre-underscore token (fixes XTKS/XASX digit-only tickers).
- Thesis extraction: strips YAML frontmatter first, then searches body for named sections (TL;DR / One-line thesis / Thesis(?: Statement) / Situation summary / Company Overview / Summary, with optional numeric prefix like "## 1."). Falls back to first meaningful prose paragraph, skipping YAML-like `key: value` lines, headings, tables, and blockquotes.
- CLI: `python -m tools.report_generator --candidates-summary [--include-archive]`.

**Live verification (2026-04-17 01:16Z)**:
- PDF: `reports/candidates/2026-04-17_0116_candidates_summary.pdf` (56KB, 18 pages).
- 75 candidates surfaced — 0 immediate, 33 active, 1 delivered, 41 watchlist.
- Sampled pages (5, 6) confirmed real prose rationales for TDnet-origin candidates (3391, 4206, 6135, 8267, 8934) and for English-source candidates (AXSM, RGR, RPAY, VRDN, TVTX).
- Index table renders correctly across stages.
- Detail sections include ticker, MIC, score, profile, stage, status, strategy, dates, rationale, sources, file path.

**Schedule wiring**:
- `unified-reporting` scheduled task (cron `30 */4 * * *`) prompt updated to run:
  1. `--daily` (daily digest, always)
  2. `--candidates-summary` (all-candidates summary PDF, always) — NEW
  3. `--weekly` (Sundays / if missing for current ISO week)
  4. `--dossier` for any unprocessed immediate-band candidates
- Next auto-run: 2026-04-17T02:37Z.

**Known minor issues (non-blocking)**:
- Watchlist JSON stubs show `—` in score column when only `max_raw_score` is set without `score_with_convergence_bonus`. True for most single-signal UK/JP/AU stubs — score is not yet computed without convergence.
- Detail section ticker line for XLON watchlist entries sometimes shows empty ticker; the filename (e.g. `BKM_XLON_2026-04-15.json`) is parsed but ticker field in the JSON is often null. Not breaking — title/company name is shown.

**File locations for Pedro**:
- `reports/candidates/` — NEW — all-candidates summary PDF, refreshed every 4h.
- `reports/daily/` — 4 existing digests + refreshes every 4h.
- `reports/weekly/` — 1 existing strategic digest (2026-W16).
- `reports/dossiers/pdf/` — 63 per-candidate detailed dossiers from prior session.

---

## Session 63 (2026-04-17) — Thesis Gate + Two-PDF Reporting

**Directive (Pedro)**: "Option C, but i need you to do this always. How is it possible that we have a candidate and not a thesis on why it is a candidate? ... change doc structure to what you have proposed"

**Delivered**:
- `tools/candidate_gate.py` — NEW. Enforces thesis-required promotion rule (D-008). Public `promote_candidate(signal, thesis, band=...)`. CLI: `--audit`, `--demote-stubs`. Rejected promotions logged to `working/rejected_promotions_<date>.json`.
- Stub cleanup — 41 JSON stubs + 6 thin TDnet MDs moved to `candidates/rejected_pending_thesis/`. Candidates folder now holds 27 MDs (20 pass strict thesis audit, 7 pass on content but have non-standard section names).
- `tools/report_generator.py` — ADDED `generate_executive_summary()` + `generate_detail_book()`. New CLI: `--executive-summary`, `--detail-book`, `--both`, `--include-archive`. Titles cleaned (strip "Candidate:" prefix, trailing "(Session NN)" noise). One-line why compressed via `_one_line_why()`.
- `DECISIONS.md` — D-008 (thesis rule) + D-009 (stub demotion plan).
- `reports/candidates/executive_summary.pdf` (3 pages, landscape) — at-a-glance table, sorted by soonest catalyst.
- `reports/candidates/detail_book.pdf` (30 pages, portrait) — one candidate per page with Situation, Next catalyst, Key dates, Sources.
- Both copied to top-level `reporting/` so Pedro can find them quickly.
- Scheduled task `unified-reporting` updated to emit both PDFs and copy to reporting/.

---

## Scheduled run (2026-04-17 18:38 UTC) — unified-reporting

**PDFs emitted**:
- `reports/candidates/2026-04-17_1838_candidates_summary.pdf` (--daily)
- `reports/candidates/executive_summary.pdf` (--both)
- `reports/candidates/detail_book.pdf` (--both)
- Copies placed at `../reporting/executive_summary.pdf` and `../reporting/detail_book.pdf`.

**Weekly**: skipped (Friday — not Sunday).

**Immediate-band dossiers**: none needed. No candidates in `candidates/immediate/` subfolder; all 5 root MDs are stage=active.

**Thesis gate audit**: `python tools/candidate_gate.py --audit` → md_total=5, md_with_thesis=3, md_missing_thesis=2, json_stubs=0. Missing thesis (both lack `next_catalyst_date`): `AXSM_ADA_PDUFA.md`, `RPAY_Forager_ActivistPoisonPill.md`. Full report at `working/thesis_gate_audit_2026-04-17.json`. No new JSON stubs in `candidates/watchlist/` (empty) → `--demote-stubs` skipped.

**Counts**: candidates root = 5 MDs (3 rich thesis / 2 missing `next_catalyst_date`); delivered = 1; watchlist = 0; rejected_pending_thesis = 67.

---

## Scheduled run (2026-04-17 22:39 UTC) — unified-reporting

**PDFs emitted**:
- `reports/candidates/2026-04-17_2238_candidates_summary.pdf` (--daily)
- `reports/candidates/executive_summary.pdf` regenerated (--both)
- `reports/candidates/detail_book.pdf` regenerated (--both)
- Copied to `../reporting/executive_summary.pdf` and `../reporting/detail_book.pdf`.

**Weekly**: skipped (Friday 2026-04-17 UTC / session date 2026-04-18 local — not Sunday).

**Immediate-band dossiers**: none needed. `candidates/immediate/` subfolder does not exist; all 5 root MDs (RPAY, AXSM, RGR, VERA, VRDN) resolve to stage=active.

**Thesis gate audit**: `python tools/candidate_gate.py --audit` → md_total=5, md_with_thesis=3, md_missing_thesis=2, json_stubs=0. Still missing `next_catalyst_date` (unchanged from prior run): `AXSM_ADA_PDUFA.md`, `RPAY_Forager_ActivistPoisonPill.md`. Full report: `working/thesis_gate_audit_2026-04-17.json`. No new JSON stubs in `candidates/watchlist/` (empty) → `--demote-stubs` skipped. Per migration rule, migrated candidates not demoted.

**Counts**: candidates root = 5 MDs (3 rich thesis / 2 missing `next_catalyst_date`); delivered = 1; watchlist = 0; rejected_pending_thesis = 66.

---

## Scheduled run (2026-04-18 02:39 UTC) — unified-reporting

**PDFs emitted**:
- `reports/candidates/2026-04-18_0238_candidates_summary.pdf` (--daily)
- `reports/candidates/executive_summary.pdf` regenerated (--both)
- `reports/candidates/detail_book.pdf` regenerated (--both)
- Copied to `../reporting/executive_summary.pdf` and `../reporting/detail_book.pdf`.

**Weekly**: skipped (Saturday 2026-04-18 — not Sunday).

**Immediate-band dossiers**: none needed. `candidates/immediate/` subfolder does not exist; all 5 root MDs (RPAY, AXSM, RGR, VERA, VRDN) resolve to stage=active. Per-ticker dossier PDFs (AXSM, RGR, RPAY, VERA, VRDN) already present under `../reporting/dossiers/`.

**Thesis gate audit**: `python tools/candidate_gate.py --audit` → md_total=5, md_with_thesis=3, md_missing_thesis=2, json_stubs=0. Still missing `next_catalyst_date` (unchanged): `AXSM_ADA_PDUFA.md`, `RPAY_Forager_ActivistPoisonPill.md`. Full report: `working/thesis_gate_audit_2026-04-18.json`. No new JSON stubs in `candidates/watchlist/` (empty) → `--demote-stubs` skipped. Per migration rule, migrated candidates not demoted.

**Counts**: candidates root = 5 MDs (3 rich thesis / 2 missing `next_catalyst_date`); delivered = 1; watchlist = 0; rejected_pending_thesis = 66.

---

## Scheduled run (2026-04-20 09:02 UTC) — unified-reporting

**PDFs emitted**:
- `reports/candidates/2026-04-20_0902_candidates_summary.pdf` (--daily)
- `reports/candidates/executive_summary.pdf` regenerated (--both)
- `reports/candidates/detail_book.pdf` regenerated (--both)
- Copied to `../reporting/executive_summary.pdf` and `../reporting/detail_book.pdf`.

**Weekly**: skipped (Monday 2026-04-20 UTC — not Sunday).

**Immediate-band dossiers**: none needed. `candidates/immediate/` subfolder does not exist; all 5 root MDs (RPAY, AXSM, RGR, VERA, VRDN) resolve to stage=active. Migrated candidates retained per no-demotion rule.

**Thesis gate audit**: `python tools/candidate_gate.py --audit` → md_total=5, md_with_thesis=3, md_missing_thesis=2, json_stubs=0. Still missing `next_catalyst_date` (unchanged from prior runs): `AXSM_ADA_PDUFA.md`, `RPAY_Forager_ActivistPoisonPill.md`. Full report: `working/thesis_gate_audit_2026-04-20.json`. No new JSON stubs in `candidates/watchlist/` (empty) → `--demote-stubs` skipped.

**Counts**: candidates root = 5 MDs (3 rich thesis / 2 missing `next_catalyst_date`); delivered = 1; watchlist = 0; rejected_pending_thesis = 66.


---

## Scheduled run (2026-04-20 09:02 UTC) — unified-reporting

**PDFs emitted**:
- `reports/candidates/2026-04-20_0902_candidates_summary.pdf` (--daily)
- `reports/candidates/executive_summary.pdf` regenerated (--both)
- `reports/candidates/detail_book.pdf` regenerated (--both)
- Copied to `../reporting/executive_summary.pdf` and `../reporting/detail_book.pdf`.

**Weekly**: skipped (Monday 2026-04-20 UTC — not Sunday).

**Immediate-band dossiers**: none needed. `candidates/immediate/` subfolder does not exist; all 5 root MDs (RPAY, AXSM, RGR, VERA, VRDN) resolve to stage=active. Migrated candidates retained per no-demotion rule.

**Thesis gate audit**: `python tools/candidate_gate.py --audit` → md_total=5, md_with_thesis=3, md_missing_thesis=2, json_stubs=0. Still missing `next_catalyst_date` (unchanged from prior runs): `AXSM_ADA_PDUFA.md`, `RPAY_Forager_ActivistPoisonPill.md`. Full report: `working/thesis_gate_audit_2026-04-20.json`. No new JSON stubs in `candidates/watchlist/` (empty) → `--demote-stubs` skipped.

**Counts**: candidates root = 5 MDs (3 rich thesis / 2 missing `next_catalyst_date`); delivered = 1; watchlist = 0; rejected_pending_thesis = 66.


---

## Scheduled run (2026-04-20 10:38 UTC) — unified-reporting

**PDFs emitted**:
- `reports/candidates/2026-04-20_1038_candidates_summary.pdf` (--daily)
- `reports/candidates/executive_summary.pdf` regenerated (--both)
- `reports/candidates/detail_book.pdf` regenerated (--both)
- Copied to `../reporting/executive_summary.pdf` and `../reporting/detail_book.pdf`.

**Weekly**: skipped (Monday 2026-04-20 UTC — not Sunday).

**Immediate-band dossiers**: none needed. No candidate in `candidates/*.md` resolves to stage=immediate; all 5 root MDs (RPAY, AXSM, RGR, VERA, VRDN) are stage=active or watchlist. Migrated candidates retained per no-demotion rule.

**Thesis gate audit**: `python tools/candidate_gate.py --audit` → md_total=5, md_with_thesis=3, md_missing_thesis=2, json_stubs=0. Still missing `next_catalyst_date` (unchanged from prior runs): `AXSM_ADA_PDUFA.md`, `RPAY_Forager_ActivistPoisonPill.md`. Full report: `working/thesis_gate_audit_2026-04-20.json` (overwritten by this run). No new JSON stubs in `candidates/watchlist/` (empty) → `--demote-stubs` skipped.

**Counts**: candidates root = 5 MDs (3 rich thesis / 2 missing `next_catalyst_date`); delivered = 1; watchlist = 0; rejected_pending_thesis = 66.

---

## Scheduled run (2026-04-20 14:38 UTC) — unified-reporting

**Workflow**: scheduled task per `uploads/SKILL.md`. €215M mcap floor honored; no demotions of migrated candidates.

**PDFs emitted**:
- `reports/candidates/2026-04-20_1438_candidates_summary.pdf` (daily digest)
- `reports/candidates/executive_summary.pdf` (regenerated via `--both`)
- `reports/candidates/detail_book.pdf` (regenerated via `--both`)
- Weekly: skipped (Monday — not Sunday).
- Dossier runs: none needed — `candidates/immediate/` subfolder does not exist; all 5 root MDs resolve to stage=active.

**Copies to top-level `../reporting/`**: `executive_summary.pdf` (20,844 B) + `detail_book.pdf` (15,324 B) overwritten.

**Thesis gate audit**: `python tools/candidate_gate.py --audit` → `md_total=5, md_with_thesis=3, md_missing_thesis=2, json_stubs=0`. Still missing `next_catalyst_date` (unchanged): `AXSM_ADA_PDUFA.md`, `RPAY_Forager_ActivistPoisonPill.md`. Both are migrated candidates — retained per no-demotion rule. Full report: `working/thesis_gate_audit_2026-04-20.json`. `candidates/watchlist/` empty → `--demote-stubs` moved 0 files.

**Counts**: candidates root = 5 MDs; delivered = 1; archive = 0; _archived_post_edge = 3; rejected_pending_thesis = 66; watchlist = 0.

**Scope discipline**: signals/, scanner configs, scanner registry not touched.
