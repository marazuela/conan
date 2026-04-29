"""
supabase_client — thin HTTP wrapper used by Modal scanner functions.

Talks to two Supabase surfaces:
  1. PostgREST at {SUPABASE_URL}/rest/v1/... for row CRUD.
  2. Storage at {SUPABASE_URL}/storage/v1/object/... for scanner-cache + filing bodies.

Authenticates as service role. RLS is bypassed (per spec.md §3.4: INSERT into signals is
service_role-only; same for filings, scanner_runs). Modal workers never hold a user JWT.

Public API matches spec.md §7.1 with the subset needed by Phase 1 scanners:
  SupabaseClient()                       # reads SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY
  .load_scanner_config(name) -> ScannerConfig
  .load_scanner_statuses(names) -> dict[name,status]
  .open_scanner_run(scanner_id) -> str   # returns run_id
  .close_scanner_run(run_id, status, signals_emitted, errors)
  .upsert_filing(filing) -> str          # returns filing id (UUID)
  .insert_signals(signals) -> list[str]  # returns signal_ids of rows actually inserted
  .resolve_or_create_entity(hints) -> str # returns entity_id
  .read_cache(prefix, key) -> Optional[bytes]
  .write_cache(prefix, key, data)
  .load_rubric_version_id(profile) -> str # current active version

Not covered yet (deferred to reactor edge function / candidate-gate proxy):
  .update_signal_convergence() — reactor writes convergence_* columns directly in SQL.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests


@dataclass
class ScannerConfig:
    scanner_id: str
    name: str
    status: str
    geography: Optional[str]
    cadence: str
    default_scoring_profile: str
    signal_type_profile_map: Dict[str, str]
    endpoints: Dict[str, Any]
    timeout_soft_s: int
    timeout_hard_s: int
    config: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EntityHints:
    """Fields a scanner has about an issuer; used by resolve_or_create_entity to walk the
    fallback chain (figi → ticker+mic → codigo_cvm → id_empresa_biva → stock_code → cik
    → cnpj → isin → normalized name). Whichever fields are non-None get checked in priority
    order; first hit wins. If none resolve, a new entity row is created with name_normalized
    as a fallback identifier."""
    issuer_figi: Optional[str] = None
    ticker: Optional[str] = None
    mic: Optional[str] = None
    cik: Optional[str] = None
    cnpj: Optional[str] = None
    isin: Optional[str] = None
    codigo_cvm: Optional[str] = None
    id_empresa_biva: Optional[str] = None
    stock_code: Optional[str] = None
    name: Optional[str] = None
    country: Optional[str] = None


def _looks_like_ticker(value: str) -> bool:
    """Reject obvious non-ticker strings — FIGIs (BBGnnn..., 12 chars) and
    ISINs (12 alphanumeric). Exists because a pre-fix cached FigiResolution
    could still surface a stale `ticker_local` holding the ISIN/FIGI input;
    without this guard, _backfill_entity would write that string into
    entities.primary_ticker."""
    v = value.strip()
    if not v or len(v) > 10:
        return False
    if len(v) == 12 and v.isalnum():
        # ISIN shape: 2-letter country + 10 alphanumeric. FIGI shape starts BBG.
        return False
    if v.startswith("BBG") and len(v) >= 10:
        return False
    return True


class SupabaseError(RuntimeError):
    """Raised when Supabase returns a non-2xx/3xx response. Contains status + body."""
    def __init__(self, status: int, body: str):
        super().__init__(f"supabase {status}: {body[:400]}")
        self.status = status
        self.body = body


class SupabaseClient:
    def __init__(self, url: Optional[str] = None, service_key: Optional[str] = None, timeout: float = 15.0):
        self.url = (url or os.environ["SUPABASE_URL"]).rstrip("/")
        self.service_key = service_key or os.environ["SUPABASE_SERVICE_ROLE_KEY"]
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({
            "apikey": self.service_key,
            "Authorization": f"Bearer {self.service_key}",
        })

    # ------------------------------------------------------------------
    # PostgREST helpers
    # ------------------------------------------------------------------

    def _rest(self, method: str, path: str, *, params: Optional[Dict[str, Any]] = None,
              json_body: Optional[Any] = None, prefer: Optional[str] = None) -> Any:
        headers: Dict[str, str] = {}
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        if prefer:
            headers["Prefer"] = prefer
        r = self._session.request(
            method, f"{self.url}/rest/v1/{path}",
            params=params, json=json_body, headers=headers, timeout=self.timeout,
        )
        if r.status_code >= 400:
            raise SupabaseError(r.status_code, r.text)
        if not r.content:
            return None
        try:
            return r.json()
        except ValueError:
            return r.text

    def _is_retryable_error(self, exc: Exception) -> bool:
        if isinstance(exc, requests.exceptions.RequestException):
            return True
        return isinstance(exc, SupabaseError) and (exc.status == 429 or exc.status >= 500)

    def _rest_with_retry(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Any] = None,
        prefer: Optional[str] = None,
        attempts: int = 3,
        backoff_s: float = 0.25,
    ) -> Any:
        for attempt in range(attempts):
            try:
                return self._rest(
                    method,
                    path,
                    params=params,
                    json_body=json_body,
                    prefer=prefer,
                )
            except (SupabaseError, requests.exceptions.RequestException) as exc:
                if attempt == attempts - 1 or not self._is_retryable_error(exc):
                    raise
                time.sleep(backoff_s * (2 ** attempt))
        return None

    # ------------------------------------------------------------------
    # Scanner registry
    # ------------------------------------------------------------------

    def load_scanner_config(self, scanner_name: str) -> ScannerConfig:
        rows = self._rest("GET", "scanners",
                          params={"name": f"eq.{scanner_name}", "select": "*", "limit": 1})
        if not rows:
            raise SupabaseError(404, f"scanner '{scanner_name}' not found in registry")
        r = rows[0]
        return ScannerConfig(
            scanner_id=r["id"],
            name=r["name"],
            status=r["status"],
            geography=r.get("geography"),
            cadence=r["cadence"],
            default_scoring_profile=r["default_scoring_profile"],
            signal_type_profile_map=r.get("signal_type_profile_map") or {},
            endpoints=r.get("endpoints") or {},
            timeout_soft_s=r["timeout_soft_s"],
            timeout_hard_s=r["timeout_hard_s"],
            config=r.get("config") or {},
        )

    def load_scanner_statuses(self, scanner_names: List[str]) -> Dict[str, str]:
        unique = sorted({name for name in scanner_names if name})
        if not unique:
            return {}
        in_clause = ",".join(f'"{name}"' for name in unique)
        rows = self._rest(
            "GET",
            "scanners",
            params={"name": f"in.({in_clause})", "select": "name,status"},
        ) or []
        return {
            row["name"]: row["status"]
            for row in rows
            if row.get("name") and row.get("status")
        }

    def load_operational_daily_names_for_hour(self, hour_utc: int,
                                              null_default_hour: int = 13) -> List[str]:
        """Names of `cadence='daily'` scanners operational right now and scheduled
        for `hour_utc`. Rows with NULL `scheduled_hour_utc` route to `null_default_hour`
        so a newly-registered scanner still fires once a day without manual timing.

        Used by `dispatch_release_times` in modal_workers/app.py.
        """
        # Split into two REST calls because PostgREST can't OR a nullable `eq` with
        # a NULL filter on the same column in a single `or=` expression cleanly;
        # the two-query pattern is simpler and runs in milliseconds.
        matched = self._rest(
            "GET",
            "scanners",
            params={
                "cadence": "eq.daily",
                "status": "eq.operational",
                "scheduled_hour_utc": f"eq.{hour_utc}",
                "select": "name",
            },
        ) or []
        names = [row["name"] for row in matched if row.get("name")]
        if hour_utc == null_default_hour:
            unset = self._rest(
                "GET",
                "scanners",
                params={
                    "cadence": "eq.daily",
                    "status": "eq.operational",
                    "scheduled_hour_utc": "is.null",
                    "select": "name",
                },
            ) or []
            names.extend(row["name"] for row in unset if row.get("name"))
        return sorted(set(names))

    def update_scanner_last_run(self, scanner_id: str, last_run_utc: str,
                                last_run_status: str, last_run_signals: int) -> None:
        self._rest_with_retry("PATCH", "scanners",
                              params={"id": f"eq.{scanner_id}"},
                              json_body={"last_run_utc": last_run_utc,
                                         "last_run_status": last_run_status,
                                         "last_run_signals": last_run_signals})

    # ------------------------------------------------------------------
    # Scanner runs
    # ------------------------------------------------------------------

    def open_scanner_run(self, scanner_id: str, modal_invocation_id: Optional[str] = None) -> str:
        body = {"scanner_id": scanner_id, "status": "running"}
        if modal_invocation_id:
            body["modal_invocation_id"] = modal_invocation_id
        rows = self._rest("POST", "scanner_runs", json_body=body,
                          prefer="return=representation")
        return rows[0]["id"]

    def close_scanner_run(self, run_id: str, *, status: str, signals_emitted: int = 0,
                          fetched_records: Optional[int] = None,
                          errors: Optional[List[Any]] = None, raw_log_path: Optional[str] = None) -> None:
        patch: Dict[str, Any] = {
            "status": status,
            "signals_emitted": signals_emitted,
            "completed_at": "now()",
            "errors": errors or [],
        }
        if fetched_records is not None:
            patch["fetched_records"] = fetched_records
        if raw_log_path:
            patch["raw_log_path"] = raw_log_path
        self._rest_with_retry("PATCH", "scanner_runs",
                              params={"id": f"eq.{run_id}"},
                              json_body=patch)

    @staticmethod
    def _parse_iso(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None

    def reap_orphan_runs(self, max_age_seconds: int = 1200) -> List[Dict[str, Any]]:
        """Sweep `scanner_runs` where status='running' AND started_at is older than
        `max_age_seconds`. Flip them to status='timeout' with an errors note.

        Background (v2 memory, 2026-04-20): Modal hard-timeouts leave scanner_runs
        orphaned at status='running' when the container is killed before the scanner
        can call close_scanner_run. This helper is invoked by the dispatchers (app.py
        `_dispatch()`) before each bucket's spawn wave so the dashboard's scanner-
        health card doesn't report phantom active runs.

        Atomic under concurrency: the UPDATE with WHERE status='running' AND started_at
        predicate is race-safe — two reapers running simultaneously will both see the
        same candidate set and the second one's UPDATE is a no-op (rows already flipped).

        Returns the rows that were reaped (scanner_id + run_id + age in seconds) so
        the dispatcher can report counts.
        """
        now_ts = datetime.now(timezone.utc).timestamp()
        cutoff = datetime.fromtimestamp(now_ts - max_age_seconds, tz=timezone.utc).isoformat()

        # Gather candidates first so we can return age info + scanner_ids.
        candidates = self._rest(
            "GET", "scanner_runs",
            params={
                "select": "id,scanner_id,started_at",
                "status": "eq.running",
                "started_at": f"lt.{cutoff}",
                "limit": "100",
            },
        )
        if not candidates:
            return []

        ids = ",".join(f'"{row["id"]}"' for row in candidates)
        patch = {
            "status": "timeout",
            "completed_at": "now()",
            "errors": [{"type": "orphan_reaper",
                        "message": f"marked timeout: status=running for >{max_age_seconds}s; reaped by next dispatcher"}],
        }
        self._rest_with_retry(
            "PATCH", "scanner_runs",
            params={"id": f"in.({ids})", "status": "eq.running"},
            json_body=patch,
        )

        latest_started_by_scanner: Dict[str, str] = {}
        for row in candidates:
            scanner_id = row["scanner_id"]
            started_at = row["started_at"]
            current = latest_started_by_scanner.get(scanner_id)
            if current is None or started_at > current:
                latest_started_by_scanner[scanner_id] = started_at

        reconciled_scanners: Dict[str, bool] = {}
        if latest_started_by_scanner:
            scanner_ids = ",".join(f'"{scanner_id}"' for scanner_id in latest_started_by_scanner)
            scanner_rows = self._rest(
                "GET", "scanners",
                params={"select": "id,last_run_utc", "id": f"in.({scanner_ids})"},
            ) or []
            scanner_by_id = {row["id"]: row for row in scanner_rows}
            timeout_at = datetime.now(timezone.utc).isoformat()

            for scanner_id, started_at in latest_started_by_scanner.items():
                scanner_row = scanner_by_id.get(scanner_id) or {}
                last_run_dt = self._parse_iso(scanner_row.get("last_run_utc"))
                orphan_started_dt = self._parse_iso(started_at)
                if last_run_dt and orphan_started_dt and last_run_dt > orphan_started_dt:
                    reconciled_scanners[scanner_id] = False
                    continue
                try:
                    self.update_scanner_last_run(
                        scanner_id,
                        last_run_utc=timeout_at,
                        last_run_status="timeout",
                        last_run_signals=0,
                    )
                    reconciled_scanners[scanner_id] = True
                except (SupabaseError, requests.exceptions.RequestException):
                    reconciled_scanners[scanner_id] = False

        return [
            {
                "id": r["id"],
                "scanner_id": r["scanner_id"],
                "started_at": r["started_at"],
                "scanner_reconciled": reconciled_scanners.get(r["scanner_id"], False),
            }
            for r in candidates
        ]

    # ------------------------------------------------------------------
    # Rubric lookup
    # ------------------------------------------------------------------

    def load_rubric_version_id(self, profile: str) -> str:
        """Active rubric_version_id for a profile (superseded_at IS NULL). Cached per-process."""
        if not hasattr(self, "_rubric_cache"):
            self._rubric_cache: Dict[str, str] = {}
        if profile in self._rubric_cache:
            return self._rubric_cache[profile]
        rows = self._rest("GET", "rubrics",
                          params={"profile": f"eq.{profile}",
                                  "superseded_at": "is.null",
                                  "select": "id", "limit": 1})
        if not rows:
            raise SupabaseError(404, f"no active rubric for profile '{profile}'")
        rid = rows[0]["id"]
        self._rubric_cache[profile] = rid
        return rid

    # ------------------------------------------------------------------
    # Entities (resolve-or-create via the fallback chain)
    # ------------------------------------------------------------------

    def _backfill_entity(self, row: Dict[str, Any], hints: EntityHints) -> None:
        """PATCH an existing entity with any hint columns it's missing.

        resolve_or_create_entity's original contract was: match or create. That left
        entities created by the first scanner to hit an issuer stuck with whatever
        narrow identifier set that scanner had (e.g., ESMA matched by FIGI but never
        populated primary_ticker because the scanner didn't pass one). The dashboard
        signals table renders primary_ticker, so those rows showed blank.

        This helper PATCHes only NULL columns, so a later scanner carrying richer
        hints back-populates without clobbering anything already set. Best-effort:
        errors are swallowed — the caller still gets entity_id and the signal path
        keeps moving."""
        patch: Dict[str, Any] = {}
        if hints.ticker and not row.get("primary_ticker") and _looks_like_ticker(hints.ticker):
            patch["primary_ticker"] = hints.ticker
        if hints.mic and not row.get("primary_mic"):
            patch["primary_mic"] = hints.mic
        if hints.name and not row.get("name"):
            patch["name"] = hints.name
        if hints.country and not row.get("country"):
            patch["country"] = hints.country
        if not patch:
            return
        try:
            self._rest("PATCH", "entities",
                       params={"id": f"eq.{row['id']}"},
                       json_body=patch)
        except SupabaseError:
            pass  # best-effort; not fatal for signal path

    def prefetch_entities_by_figi(self, figis: List[str]) -> Dict[str, Dict[str, Any]]:
        """Bulk-GET entities whose issuer_figi is in `figis`, in a single request.

        Returns a dict keyed by issuer_figi, value = full entity row (id, name,
        primary_ticker, etc.). resolve_or_create_entity's first priority is a
        FIGI lookup — pre-warming this map cuts a 2000-signal cold-start (ESMA)
        from 2000 round trips (~400s in EU-West → eu-west-3) to one.

        Callers that find a hit can skip resolve_or_create_entity's lookup phase
        and go straight to _backfill_entity + return. Callers that find no hit
        fall through to the normal per-signal path."""
        if not figis:
            return {}
        unique = sorted({f for f in figis if f})
        if not unique:
            return {}
        # PostgREST `in.(a,b,c)` — URL-encoded list. Chunk at 200 per request so
        # the querystring stays well under the typical 8KB header cap (FIGIs are
        # 12 chars each; 200 * 13 = 2600 chars + overhead).
        result: Dict[str, Dict[str, Any]] = {}
        for i in range(0, len(unique), 200):
            chunk = unique[i:i + 200]
            in_clause = ",".join(chunk)
            rows = self._rest("GET", "entities",
                              params={"issuer_figi": f"in.({in_clause})",
                                      "select": "id,issuer_figi,name,primary_ticker,primary_mic,country"})
            for r in rows or []:
                result[r["issuer_figi"]] = r
        return result

    def resolve_or_create_entity(self, hints: EntityHints,
                                 prefetched: Optional[Dict[str, Dict[str, Any]]] = None) -> str:
        """Try each identifier in priority order; return entity_id. If nothing matches,
        create a new entity and register whichever identifiers are non-None."""
        candidates: List[tuple[str, str, int]] = []
        if hints.issuer_figi:
            candidates.append(("ticker_mic", hints.issuer_figi, 10))  # FIGI registered as ticker_mic w/ issuer_figi col
        if hints.ticker and hints.mic:
            candidates.append(("ticker_mic", f"{hints.ticker}@{hints.mic}", 20))
        if hints.codigo_cvm:
            candidates.append(("codigo_cvm", hints.codigo_cvm, 30))
        if hints.id_empresa_biva:
            candidates.append(("id_empresa_biva", hints.id_empresa_biva, 40))
        if hints.stock_code:
            candidates.append(("stock_code", hints.stock_code, 50))
        if hints.cik:
            candidates.append(("cik", hints.cik, 60))
        if hints.cnpj:
            candidates.append(("cnpj", hints.cnpj, 70))
        if hints.isin:
            candidates.append(("isin", hints.isin, 80))
        if hints.name:
            candidates.append(("name_normalized", hints.name.strip().lower(), 90))

        # Priority 1: lookup by issuer_figi column directly on entities (most accurate).
        if hints.issuer_figi:
            # Prefer the caller's prefetched map (single bulk GET) over a per-
            # call round trip. Falls through to the per-call path if absent.
            if prefetched is not None and hints.issuer_figi in prefetched:
                hit = prefetched[hints.issuer_figi]
                self._backfill_entity(hit, hints)
                return hit["id"]
            rows = self._rest("GET", "entities",
                              params={"issuer_figi": f"eq.{hints.issuer_figi}",
                                      "select": "id,primary_ticker,primary_mic,name,country",
                                      "limit": 1})
            if rows:
                self._backfill_entity(rows[0], hints)
                return rows[0]["id"]

        # Priority 2+: walk entity_identifiers.
        for id_type, id_value, _ in candidates:
            if id_type == "ticker_mic" and hints.issuer_figi:
                continue  # handled above
            rows = self._rest("GET", "entity_identifiers",
                              params={"id_type": f"eq.{id_type}",
                                      "id_value": f"eq.{id_value}",
                                      "select": "entity_id", "limit": 1})
            if rows:
                return rows[0]["entity_id"]

        # Nothing matched — create the entity and register every identifier we have.
        new_rows = self._rest("POST", "entities",
                              json_body={"issuer_figi": hints.issuer_figi,
                                         "name": hints.name or (hints.ticker or "unknown"),
                                         "primary_ticker": hints.ticker,
                                         "primary_mic": hints.mic,
                                         "country": hints.country},
                              prefer="return=representation")
        entity_id = new_rows[0]["id"]
        identifier_rows = []
        for id_type, id_value, priority in candidates:
            if id_type == "ticker_mic" and hints.issuer_figi and id_value == hints.issuer_figi:
                continue
            identifier_rows.append({"entity_id": entity_id, "id_type": id_type,
                                    "id_value": id_value, "priority": priority})
        if identifier_rows:
            try:
                self._rest("POST", "entity_identifiers", json_body=identifier_rows,
                           prefer="return=minimal")
            except SupabaseError as e:
                # Unique-constraint collisions mean another process created the same
                # identifier concurrently. Not fatal; entity is already findable by it.
                if e.status not in (409, 23505):
                    raise
        return entity_id

    # ------------------------------------------------------------------
    # Filings
    # ------------------------------------------------------------------

    def upsert_filing(self, filing: Dict[str, Any]) -> str:
        """Insert a filing row; on source_content_hash conflict return the existing id."""
        rows = self._rest("POST", "filings", json_body=filing,
                          prefer="return=representation,resolution=merge-duplicates")
        return rows[0]["id"]

    # ------------------------------------------------------------------
    # Signals
    # ------------------------------------------------------------------

    def insert_signals(self, signals: List[Dict[str, Any]]) -> List[str]:
        """Bulk insert. Duplicates (source_content_hash, scoring_profile) are silently
        skipped via ON CONFLICT DO NOTHING. Returns the signal_ids of rows actually
        inserted — callers can diff against len(signals) to learn how many were dup.

        `on_conflict` is explicit: PostgREST otherwise falls through to the PK
        (signal_id), which isn't the dedup axis we want (scanner may regenerate a
        fresh signal_id for the same content, so we'd happily insert duplicates).
        The ESMA run that surfaced this produced ERROR 42P10 on some PostgREST
        configurations; pinning the conflict target removes that ambiguity."""
        if not signals:
            return []
        rows = self._rest("POST", "signals",
                          params={"on_conflict": "source_content_hash,scoring_profile"},
                          json_body=signals,
                          prefer="return=representation,resolution=ignore-duplicates")
        if rows is None:
            return []
        return [r["signal_id"] for r in rows]

    # ------------------------------------------------------------------
    # Price tracking (signal_price_snapshots + outcomes mirror)
    # ------------------------------------------------------------------

    # Same chunking guideline as insert_signals: keep `signal_id=in.(...)` lists
    # bounded so the request line stays well under Kong's 8KB cap.
    _PRICE_TRACKER_IN_CHUNK = 200

    def load_price_tracking_subjects(self, window_days: int = 35) -> List[Dict[str, Any]]:
        """Return a flat list of subjects the price tracker should evaluate.

        Each subject is a candidate (any state) created within the window OR a
        signal in band_with_bonus IN ('immediate','watchlist') that hasn't been
        promoted to a candidate. Direction is resolved via the thesis_jobs link
        for candidates, and read directly off `signals.thesis_direction` for
        signals; defaults to 'long' if missing.

        Subject shape:
          {kind: 'candidate'|'signal', signal_id, candidate_id, ticker, mic,
           thesis_direction, created_at (iso str)}
        """
        chunk_size = self._PRICE_TRACKER_IN_CHUNK
        cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
        subjects: List[Dict[str, Any]] = []

        # ---- Candidates ----
        cand_rows = self._rest("GET", "candidates", params={
            "select": "id,ticker,mic,created_at",
            "ticker": "not.is.null",
            "created_at": f"gte.{cutoff}",
            "limit": "5000",
        }) or []

        cand_to_signal: Dict[str, str] = {}
        if cand_rows:
            cand_ids = [r["id"] for r in cand_rows]
            in_clause = ",".join(f'"{cid}"' for cid in cand_ids)
            tj_rows = self._rest("GET", "thesis_jobs", params={
                "select": "candidate_id,signal_id",
                "candidate_id": f"in.({in_clause})",
                "status": "eq.promoted",
            }) or []
            for r in tj_rows:
                if r.get("candidate_id") and r.get("signal_id"):
                    cand_to_signal[r["candidate_id"]] = r["signal_id"]

        sig_dir: Dict[str, str] = {}
        if cand_to_signal:
            sig_ids = sorted(set(cand_to_signal.values()))
            for i in range(0, len(sig_ids), chunk_size):
                chunk = sig_ids[i:i + chunk_size]
                rows = self._rest("GET", "signals", params={
                    "select": "signal_id,thesis_direction",
                    "signal_id": f"in.({','.join(chunk)})",
                }) or []
                for r in rows:
                    sig_dir[r["signal_id"]] = r.get("thesis_direction") or "long"

        for c in cand_rows:
            direction = "long"
            sid = cand_to_signal.get(c["id"])
            if sid:
                direction = sig_dir.get(sid, "long")
            subjects.append({
                "kind": "candidate",
                "signal_id": None,
                "candidate_id": c["id"],
                "ticker": c["ticker"],
                "mic": c.get("mic"),
                "thesis_direction": direction,
                "created_at": c["created_at"],
            })

        # ---- Watchlist/immediate signals not yet promoted ----
        sig_rows = self._rest("GET", "signals", params={
            "select": "signal_id,entity_id,thesis_direction,created_at",
            "band_with_bonus": "in.(immediate,watchlist)",
            "created_at": f"gte.{cutoff}",
            "limit": "5000",
        }) or []

        promoted_signal_ids: set[str] = set()
        if sig_rows:
            ids = [r["signal_id"] for r in sig_rows]
            for i in range(0, len(ids), chunk_size):
                chunk = ids[i:i + chunk_size]
                rows = self._rest("GET", "thesis_jobs", params={
                    "select": "signal_id",
                    "signal_id": f"in.({','.join(chunk)})",
                    "status": "eq.promoted",
                }) or []
                promoted_signal_ids.update(r["signal_id"] for r in rows if r.get("signal_id"))

        entity_map: Dict[str, Dict[str, Any]] = {}
        entity_ids = sorted({r["entity_id"] for r in sig_rows if r.get("entity_id")})
        for i in range(0, len(entity_ids), chunk_size):
            chunk = entity_ids[i:i + chunk_size]
            in_clause = ",".join(f'"{eid}"' for eid in chunk)
            rows = self._rest("GET", "entities", params={
                "select": "id,primary_ticker,primary_mic",
                "id": f"in.({in_clause})",
            }) or []
            for r in rows:
                entity_map[r["id"]] = r

        for r in sig_rows:
            if r["signal_id"] in promoted_signal_ids:
                continue
            ent = entity_map.get(r.get("entity_id"))
            if not ent or not ent.get("primary_ticker"):
                continue
            subjects.append({
                "kind": "signal",
                "signal_id": r["signal_id"],
                "candidate_id": None,
                "ticker": ent["primary_ticker"],
                "mic": ent.get("primary_mic"),
                "thesis_direction": r.get("thesis_direction") or "long",
                "created_at": r["created_at"],
            })

        return subjects

    def upsert_price_snapshot(self, row: Dict[str, Any]) -> None:
        """Insert-or-update a row in signal_price_snapshots, keyed on the partial
        unique index for the relevant subject column. Caller is responsible for
        passing exactly one of signal_id / candidate_id."""
        if row.get("signal_id"):
            on_conflict = "signal_id,horizon_days"
        elif row.get("candidate_id"):
            on_conflict = "candidate_id,horizon_days"
        else:
            raise ValueError("upsert_price_snapshot requires signal_id OR candidate_id")
        self._rest_with_retry(
            "POST",
            "signal_price_snapshots",
            params={"on_conflict": on_conflict},
            json_body=row,
            prefer="resolution=merge-duplicates,return=minimal",
        )

    def update_outcome_realized_move(
        self,
        candidate_id: str,
        horizon_days: int,
        signed_move_pct: Optional[float],
    ) -> None:
        """PATCH the candidate's outcomes row with the signed realized move at
        the given horizon. No-op when no outcomes row exists yet (the candidate
        hasn't transitioned to a terminal lifecycle state) — PostgREST returns
        an empty result and we don't surface that as an error.

        Sets `labeled_at = now()` to mark automation provenance. Does not write
        `labeled_by` — that column references auth.users and we're a service
        role; absence + labeled_at being set is the convention for automation.
        """
        if horizon_days not in (1, 7, 30):
            raise ValueError(f"horizon_days must be 1/7/30, got {horizon_days}")
        column = f"realized_move_{horizon_days}d"
        body: Dict[str, Any] = {column: signed_move_pct, "labeled_at": "now()"}
        self._rest_with_retry(
            "PATCH",
            "outcomes",
            params={"candidate_id": f"eq.{candidate_id}"},
            json_body=body,
            prefer="return=minimal",
        )

    # ------------------------------------------------------------------
    # Storage (scanner-caches bucket)
    # ------------------------------------------------------------------

    def _storage_path(self, bucket: str, path: str) -> str:
        return f"{self.url}/storage/v1/object/{bucket}/{path.lstrip('/')}"

    def read_cache(self, prefix: str, key: str, timeout: Optional[float] = None) -> Optional[bytes]:
        """Fetch a cached blob from scanner-caches/{prefix}/{key}. Returns None when the
        object is absent. Supabase Storage's GET can signal "not found" as either HTTP
        404 OR HTTP 400 with `{"error":"not_found"}` in the body — we treat both as None.

        `timeout` overrides the client-level default. Callers that treat the cache as
        best-effort (e.g., openfigi_cache_backend) pass a short timeout so a slow
        Storage round-trip degrades to a cache-miss rather than blocking the scanner.
        """
        url = self._storage_path("scanner-caches", f"{prefix}/{key}")
        r = self._session.get(url, timeout=timeout if timeout is not None else self.timeout)
        if r.status_code == 404:
            return None
        if r.status_code == 400 and "not_found" in (r.text or ""):
            return None
        if r.status_code >= 400:
            raise SupabaseError(r.status_code, r.text)
        return r.content

    def write_cache(self, prefix: str, key: str, data: bytes, content_type: str = "application/json") -> None:
        """Upsert a blob at scanner-caches/{prefix}/{key}. Overwrites existing."""
        url = self._storage_path("scanner-caches", f"{prefix}/{key}")
        r = self._session.put(
            url, data=data, timeout=self.timeout,
            headers={"Content-Type": content_type, "x-upsert": "true"},
        )
        if r.status_code >= 400:
            raise SupabaseError(r.status_code, r.text)

    def write_filing_body(self, storage_path: str, body: bytes, content_type: str = "application/octet-stream") -> None:
        """Upload a raw filing to filings/{storage_path}."""
        url = self._storage_path("filings", storage_path)
        r = self._session.put(
            url, data=body, timeout=self.timeout,
            headers={"Content-Type": content_type, "x-upsert": "true"},
        )
        if r.status_code >= 400:
            raise SupabaseError(r.status_code, r.text)

    # ------------------------------------------------------------------
    # OpenFIGI cache backend adapter — wire into openfigi_resolver.set_cache_backend
    # ------------------------------------------------------------------

    def openfigi_cache_backend(self):
        """Return (load_fn, save_fn) callables suitable for openfigi_resolver.set_cache_backend().
        Serialises dict cache entries as JSON under scanner-caches/openfigi/{key}.json.

        Both callables are best-effort. Transient Storage slowness (e.g., the
        2026-04-21 ESMA ReadTimeout at the default 15s from Modal EU-West → Supabase
        eu-west-3) must degrade to a cache-miss rather than propagating as an
        exception — the resolver's fallback path (live OpenFIGI API call) is cheap
        and correct; we don't want a flaky cache read to abort an entire batch.

        load_fn uses a tight 4s timeout so a stalled Storage round-trip is treated
        as miss within one second of the typical cold-cache round-trip time.
        """
        client = self
        CACHE_READ_TIMEOUT_S = 4.0

        def load_fn(key: str) -> Optional[Dict[str, Any]]:
            try:
                raw = client.read_cache("openfigi", f"{key}.json", timeout=CACHE_READ_TIMEOUT_S)
            except (SupabaseError, requests.exceptions.RequestException):
                # Any transport error (timeout, connection reset, 5xx) → cache miss.
                return None
            if raw is None:
                return None
            try:
                return json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                return None

        def save_fn(key: str, data: Dict[str, Any]) -> None:
            try:
                client.write_cache("openfigi", f"{key}.json",
                                   json.dumps(data).encode("utf-8"),
                                   content_type="application/json")
            except (SupabaseError, requests.exceptions.RequestException):
                pass  # best effort — same tolerance as file-backed default

        return load_fn, save_fn
