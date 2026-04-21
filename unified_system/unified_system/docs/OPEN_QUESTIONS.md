# Open Questions

Questions that need investigation. Format: Q-NNN | title | context | current status | owner.

---

## Q-001 | Re-score all migrated candidates under new profile rubrics

**Context**: 33 candidates migrated from Tool 1 + Tool 2. Scores under legacy 7-dimension rubric are not comparable to new 5-profile system.

**Status**: Pending. Must be done in first full operational cycle. Expectation: no demotions from active (per D-002 design intent). If a candidate drops below 35, investigate the rubric calibration before demoting.

**Owner**: unified-operational task.

---

## Q-002 | CNMV (Spain) short disclosure access

**Context**: CNMV is Pedro's home market. The regulator's short-disclosure database is currently blocked in this sandbox. Tool 1 flagged this as high priority but never resolved.

**Status**: Blocked. Investigation needed on CNMV's portal URL, data format, and any auth/captcha requirements.

**Owner**: maintenance task to probe; escalate to Pedro if access requires credentials.

---

## Q-003 | TDnet FIGI defect — 5-char alphanumeric tickers

**Context**: Tokyo Stock Exchange now uses 5-char alphanumeric tickers (e.g., `469A0`, `364A0`) for new listings / preferred classes. OpenFIGI returns 404 on these when sent raw. Fix: strip trailing `0` when `len(ticker)==5` AND `ticker[3].isalpha()` — e.g., `469A0` → `469A.T`.

**Status**: Fix identified. To be implemented in `tools/openfigi_resolver.py` OR upstream in `tools/tdnet_scanner.py`. Preference: fix at resolver level so all scanners benefit.

**Owner**: Phase 1 tool builder (unified pipeline). Pedro explicitly scoped this in the plan.

---

## Q-004 | PTAB IPR scanner — WAF gating

**Context**: PTAB v2 API decommissioned. v3 is WAF-gated. Tool 3 flagged this.

**Status**: Deferred to Phase 6. Litigation scanners are lower priority than core operational.

**Owner**: Phase 6 builder.

---

## Q-005 | ITC 337 scanner — EDIS REST spec unclear

**Context**: ITC's EDIS system spec is unclear. Tool 3 flagged this.

**Status**: Deferred to Phase 6.

**Owner**: Phase 6 builder.

---

## Q-006 | SEDAR+ ca_universe.json build

**Context**: SEDAR+ scanner requires `working/ca_universe.json` to produce non-zero raw signals. Builder exists (`tools/ca_universe.py`); needs one-time invocation.

**Status**: PARTIALLY RESOLVED 2026-04-16. Ran `python3 -m tools.ca_universe --throttle 0.15 --boards tsx,tsxv --max 50` in S2; produced 25-issuer universe above $300M USD floor. `sedar_plus_scanner` flipped to `operational` in registry. NOTE: (a) ca_universe.py hardcodes a $300M floor, not the unified $215M floor (mismatch logged for future fix — any issuer above $300M is also above $215M, so current output is a conservative subset), (b) only 50-issuer probe used in S2 for speed; full refresh needs `--max` omitted or set to full TMX population (~1600 issuers). Schedule the full refresh under `unified-maintenance` when sandbox time permits.

**Owner**: unified-maintenance task for full-universe rebuild; Pedro or Claude for eventual $215M floor alignment.

---

## Q-007 | Party resolver live validation

**Context**: `party_resolver.py` from Tool 3 was coded but never validated against live EDGAR data. Litigation scoring depends on it.

**Status**: Needs a test script that resolves a known court defendant (e.g., "Repay Holdings Corporation" → CIK 0001720161) end-to-end.

**Owner**: Phase 6 builder. Can be done earlier as a standalone check.

---

## Q-008 | ESMA historical tracking — esma_snapshots/

**Context**: Current ESMA scanner produces point-in-time snapshots. Profile 4 (Short Positioning) Dimension 2 (Trend Direction) requires historical tracking — building vs. unwinding.

**Status**: Design in place (persist daily snapshots in `esma_snapshots/`, compare today vs. yesterday vs. 30-day baseline). Implementation deferred to Phase 7.

**Owner**: Phase 7 builder.

---

## Q-009 | EDGAR proxy-season whitelist

**Context**: Tool 1's EDGAR scanner was triggering too many false positives during proxy season (Mar–May) on boilerplate DEF 14A filings. Whitelist of known-routine filings needed.

**Status**: Implementation expected in Phase 1 EDGAR refactor.

**Owner**: Phase 1 (unified pipeline).

---

## Q-010 | SPAC filter for EDGAR

**Context**: Tool 1's EDGAR scanner picked up SPAC filings which aren't actionable. Filter should exclude filings where company is identifiable as SPAC (name contains "Acquisition Corp", "Holdings Trust", etc., or filed as shell company).

**Status**: Implementation expected in Phase 1.

**Owner**: Phase 1.

---

## Q-014 | Ro Khanna filter for Congressional trading

**Context**: Ro Khanna is a high-volume, low-signal filer on Capitol Trades. Includes him in output dilutes signal quality.

**Status**: Filter implemented in `congressional_trading.py` — to verify still active after migration.

**Owner**: Phase 1 verification.

---

## Q-016 | Terminal-marker validation in maintenance task

**Context**: Some tool files have been observed to truncate mid-write in rare cases. Adding a terminal marker comment (e.g., `# --- END OF FILE ---`) at the bottom of each tool, and verifying presence in maintenance, would catch truncations.

**Status**: Design agreed. Implementation = (a) add `# --- END OF FILE ---` to all tools, (b) maintenance task runs a check that each tool has the marker in the last line.

**Owner**: Phase 1 builder for the marker adds; maintenance task for the check.

---

## Q-019 | OpenDART API key required for kind_scanner

**Context**: `kind_scanner.py` promoted to operational in S2e (2026-04-16). Uses OpenDART (opendart.fss.or.kr) as the data source since KIND (kind.krx.co.kr) is client-rendered and WAF-gated — OpenDART covers the same disclosure universe (official FSS filings) and offers 20,000 req/day free. The API requires a `crtfc_key` parameter; scanner reads it from `OPENDART_KEY` env var and returns `status=auth_required` gracefully when missing. Currently returning 0 signals.

**Status**: Blocking Korean market signal intake. Pedro to register (free) at https://opendart.fss.or.kr/uss/umt/EgovMberInsertView.do, then set `OPENDART_KEY` env var. Once token is present, scanner will return `status: ok` and produce signals automatically on next scheduled run.

**Owner**: Pedro (one-time setup); unified-operational task (autonomous after token is in place).

---

## Q-018 | sedar_plus_scanner does not emit signals/{name}_output.json — RESOLVED 2026-04-16 S2h

**Context**: First operational cycle on 2026-04-16 at 18:07Z flagged `sedar_plus_scanner.last_run_status = "error"`. Investigation showed the scanner ran cleanly but its CLI entrypoint only `print()`ed signals to stdout — it never wrote `signals/sedar_plus_scanner_output.json`.

**Fix applied** (S2h): Added `NAME`, `OUT_FILE`, `_normalize_signal_for_unified_envelope()`, and `scan()` wrapper to `tools/sedar_plus_scanner.py`. `__main__` now writes the output file atomically (.tmp + `os.replace()`) and prints the one-line JSON summary every other scanner uses. The normalization wrapper promotes each internal signal from the legacy Tool-2 envelope to the unified envelope (adds `scanner_source`, `upstream_scanner`, inferred `scoring_profile` from `signal_type` prefix, mirrors `ticker_local` → `ticker`). Live dry-run with `--max 2`: output file emitted cleanly, status=ok.

**Residual**: Scanner's internal signals still carry legacy keys (`ticker_local`, `scanner="sedar"`, `ticker_plus_mic`, no `thesis_direction` on some branches, no `source_content_hash`). The normalization wrapper masks this at emission. A deeper cleanup could remove the legacy fields. Not blocking.

---

## Q-017 | CourtListener API token required

**Context**: `courtlistener_scanner.py` promoted to operational in S2 (2026-04-16). The CourtListener v4 `/dockets/` endpoint returns 401 Unauthorized for anonymous calls — token is mandatory, not just rate-limited. Scanner currently returns `status: auth_required` gracefully and emits 0 signals.

**Status**: Blocking litigation profile signal intake. Pedro to register at https://www.courtlistener.com/help/api/rest/authentication/ (free), then set `COURTLISTENER_TOKEN` env var. Once token is present, scanner will return `status: ok` and produce signals automatically on its next scheduled run.

**Owner**: Pedro (one-time setup); unified-operational task (autonomous after token is in place).
