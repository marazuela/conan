"""openFDA HTTP auth helper.

openFDA's public rate limit is dual-cap (per egress IP):
- 240 requests / minute
- 1,000 requests / day  (unauthenticated)

With a free api_key registered at https://open.fda.gov/apis/authentication/
the daily cap lifts to 120,000. The Sunday `deep_sweep_openfda` pass in
`modal_workers/ingestion/openfda_ingest.py` paginates `drug/drugsfda` +
`drug/label` aggressively and can exhaust the 1,000/day cap on a single run;
the resulting 429s / 5xx are absorbed by per-call retry+continue logic without
firing an operator flag (see memory `openfda_rate_limit_gap.md`).

Every openFDA HTTP call in `modal_workers/` must route through this module so
the api_key is appended consistently and future call sites get auth
automatically. The key is read from env var `OPENFDA_API_KEY` (Modal secret
`scanner-secrets::OPENFDA_API_KEY`). When unset (local dev, non-Modal
environments) the helpers fall back to unauthenticated requests — callers
keep working, but the daily cap reverts to 1,000.

Two call shapes exist across the eight openFDA-touching modules:

  1. params-dict callers — `requests.get(url, params={"search": ...})`.
     Augment with `openfda_auth_params()`:

         requests.get(openfda_url("drug/drugsfda.json"),
                      params={**caller_params, **openfda_auth_params()})

  2. pre-built URL callers — `url = f"{BASE}?search=…&limit=…"` then
     `requests.get(url)`. These build the query string manually because
     openFDA's elasticsearch needs literal `+` between AND/OR clauses, which
     `requests.params=` would percent-encode. Append `openfda_auth_query_suffix()`
     once the rest of the query is assembled:

         url = f"{OPENFDA_BASE}/drug/drugsfda.json?search={enc}&limit={n}"
         url += openfda_auth_query_suffix()
         requests.get(url)

For brand-new call sites prefer `openfda_get()`, which wraps both styles in
one HTTP call with sensible retry-on-429/5xx semantics.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, Mapping, Optional

import requests

OPENFDA_BASE = "https://api.fda.gov"

_DEFAULT_TIMEOUT_S = 20.0
_DEFAULT_ATTEMPTS = 3
_DEFAULT_BACKOFF_S = 0.5


def openfda_api_key() -> Optional[str]:
    """Return the configured api_key, or None when unset.

    Re-reads `os.environ` on every call so tests can mutate the environment
    between runs. The cost is one dict lookup, which is negligible compared
    to the HTTP latency of any caller.
    """
    key = os.environ.get("OPENFDA_API_KEY")
    return key or None


def openfda_auth_params() -> Dict[str, str]:
    """Auth fragment for callers that pass a `params=` dict to `requests.get`.

    Returns `{"api_key": "<key>"}` when configured, otherwise `{}` so the
    caller's spread (`{**caller_params, **openfda_auth_params()}`) is a no-op
    in unauthenticated environments.
    """
    key = openfda_api_key()
    return {"api_key": key} if key else {}


def openfda_auth_query_suffix() -> str:
    """Auth fragment for callers that pre-build a `?search=…&limit=…` query.

    Returns `"&api_key=<key>"` (leading `&` included so it concatenates onto
    an existing query string with at least one parameter) or `""` when no
    api_key is configured.
    """
    key = openfda_api_key()
    return f"&api_key={key}" if key else ""


def openfda_url(path: str) -> str:
    """Join `path` against the openFDA base URL.

    Does NOT append the api_key — the caller picks the auth fragment that
    matches its call shape (`openfda_auth_params()` for params-dict callers,
    `openfda_auth_query_suffix()` for pre-built-URL callers).
    """
    return f"{OPENFDA_BASE}/{path.lstrip('/')}"


def openfda_get(
    path: str,
    params: Optional[Mapping[str, Any]] = None,
    *,
    attempts: int = _DEFAULT_ATTEMPTS,
    backoff_s: float = _DEFAULT_BACKOFF_S,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    headers: Optional[Mapping[str, str]] = None,
    session: Optional[requests.Session] = None,
) -> Optional[Dict[str, Any]]:
    """Issue an authenticated GET to openFDA and return the parsed JSON body.

    Convenience wrapper for new code. Existing call sites that build their
    own elasticsearch URLs by hand (`fda_adcomm_pdufa`, `harvest_fda_events`,
    `openfda_drug_recalls`, `fda_pdufa_pipeline` sponsor-history path) keep
    their pre-built-URL shape and use `openfda_auth_query_suffix()` directly.

    Returns:
      - parsed dict on 2xx
      - None on 404 (openFDA's "no results" response)
      - None when the JSON body is unparseable

    Raises on persistent 429/5xx after exhausting `attempts`, and on >=400
    non-retryable codes immediately.
    """
    sess = session or requests.Session()
    url = openfda_url(path)
    merged_params: Dict[str, Any] = dict(params or {})
    merged_params.update(openfda_auth_params())
    last_exc: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            r = sess.get(url, params=merged_params, headers=dict(headers or {}),
                         timeout=timeout_s)
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            if attempt < attempts - 1:
                time.sleep(backoff_s * (2 ** attempt))
                continue
            raise
        if r.status_code == 404:
            return None
        if r.status_code == 429 or r.status_code >= 500:
            last_exc = _OpenFDAHTTPError(r.status_code, r.text)
            if attempt < attempts - 1:
                time.sleep(backoff_s * (2 ** attempt))
                continue
            raise last_exc
        if r.status_code >= 400:
            raise _OpenFDAHTTPError(r.status_code, r.text)
        try:
            return r.json()
        except ValueError:
            return None
    if last_exc is not None:
        raise last_exc
    return None


class _OpenFDAHTTPError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"openfda http {status}: {body[:200]}")
        self.status = status
        self.body = body
