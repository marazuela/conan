"""
OpenFIGI resolver for Tool 2 (Non-US Discovery System).

Per D-003: ticker + MIC is the sole entity identifier. Resolves to FIGI + issuer_figi.

OpenFIGI mapping API: https://api.openfigi.com/v3/mapping
- Public endpoint (unauthenticated): 25 requests per 6 seconds, 250 per minute.
- With free API key: higher limits. Set OPENFIGI_API_KEY env var to use.

Usage:
    from tools.openfigi_resolver import resolve_ticker_mic
    result = resolve_ticker_mic("7203", "XTKS")
    # {"figi": "BBG...", "issuer_figi": "BBG...", "name": "TOYOTA MOTOR CORP", ...}
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"
CACHE_DIR = Path(__file__).parent.parent / "working" / "openfigi_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class FigiResolution:
    ticker_local: str
    mic: str
    figi: Optional[str]
    issuer_figi: Optional[str]
    name: Optional[str]
    security_type: Optional[str]
    exchange_code: Optional[str]
    resolved: bool
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "ticker_local": self.ticker_local,
            "mic": self.mic,
            "figi": self.figi,
            "issuer_figi": self.issuer_figi,
            "name": self.name,
            "security_type": self.security_type,
            "exchange_code": self.exchange_code,
            "resolved": self.resolved,
            "error": self.error,
        }


def _cache_path(ticker: str, mic: str) -> Path:
    safe = f"{ticker.replace('/', '_')}_{mic}"
    return CACHE_DIR / f"{safe}.json"


def _load_cached(ticker: str, mic: str, max_age_seconds: int = 7 * 24 * 3600) -> Optional[dict]:
    path = _cache_path(ticker, mic)
    if not path.exists():
        return None
    age = time.time() - path.stat().st_mtime
    if age > max_age_seconds:
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _save_cache(ticker: str, mic: str, data: dict) -> None:
    path = _cache_path(ticker, mic)
    try:
        path.write_text(json.dumps(data, indent=2))
    except OSError:
        pass  # cache is best-effort


def resolve_ticker_mic(
    ticker: str,
    mic: str,
    use_cache: bool = True,
    timeout: int = 10,
) -> FigiResolution:
    """
    Resolve a ticker + MIC pair to FIGI + issuer_figi via OpenFIGI mapping API.

    Returns a FigiResolution with resolved=True on success, resolved=False on failure
    (with error message set). Never raises on API errors — always returns a result.
    """
    if use_cache:
        cached = _load_cached(ticker, mic)
        if cached is not None:
            return FigiResolution(**cached)

    headers = {"Content-Type": "application/json"}
    api_key = os.environ.get("OPENFIGI_API_KEY")
    if api_key:
        headers["X-OPENFIGI-APIKEY"] = api_key

    payload = [{"idType": "TICKER", "idValue": ticker, "micCode": mic}]

    try:
        response = requests.post(OPENFIGI_URL, headers=headers, json=payload, timeout=timeout)
    except requests.RequestException as e:
        result = FigiResolution(
            ticker_local=ticker, mic=mic, figi=None, issuer_figi=None,
            name=None, security_type=None, exchange_code=None,
            resolved=False, error=f"request_exception: {e}",
        )
        return result

    if response.status_code == 429:
        # rate-limited — back off and retry once
        time.sleep(6)
        try:
            response = requests.post(OPENFIGI_URL, headers=headers, json=payload, timeout=timeout)
        except requests.RequestException as e:
            return FigiResolution(
                ticker_local=ticker, mic=mic, figi=None, issuer_figi=None,
                name=None, security_type=None, exchange_code=None,
                resolved=False, error=f"rate_limit_retry_failed: {e}",
            )

    if response.status_code != 200:
        return FigiResolution(
            ticker_local=ticker, mic=mic, figi=None, issuer_figi=None,
            name=None, security_type=None, exchange_code=None,
            resolved=False, error=f"http_{response.status_code}: {response.text[:200]}",
        )

    try:
        data = response.json()
    except json.JSONDecodeError as e:
        return FigiResolution(
            ticker_local=ticker, mic=mic, figi=None, issuer_figi=None,
            name=None, security_type=None, exchange_code=None,
            resolved=False, error=f"json_decode: {e}",
        )

    if not data or not isinstance(data, list) or "error" in data[0] or "data" not in data[0]:
        err = data[0].get("error", "no_match") if data else "empty_response"
        return FigiResolution(
            ticker_local=ticker, mic=mic, figi=None, issuer_figi=None,
            name=None, security_type=None, exchange_code=None,
            resolved=False, error=err,
        )

    entries = data[0]["data"]
    if not entries:
        return FigiResolution(
            ticker_local=ticker, mic=mic, figi=None, issuer_figi=None,
            name=None, security_type=None, exchange_code=None,
            resolved=False, error="no_entries",
        )

    # Prefer Common Stock / Equity security type when multiple match.
    preferred = None
    for entry in entries:
        stype = (entry.get("securityType2") or entry.get("securityType") or "").lower()
        if "common" in stype or "equity" in stype or "depositary" in stype:
            preferred = entry
            break
    if preferred is None:
        preferred = entries[0]

    result = FigiResolution(
        ticker_local=ticker,
        mic=mic,
        figi=preferred.get("figi"),
        issuer_figi=preferred.get("compositeFIGI") or preferred.get("shareClassFIGI"),
        name=preferred.get("name"),
        security_type=preferred.get("securityType2") or preferred.get("securityType"),
        exchange_code=preferred.get("exchCode"),
        resolved=bool(preferred.get("figi")),
        error=None,
    )

    if use_cache and result.resolved:
        _save_cache(ticker, mic, result.to_dict())

    return result


if __name__ == "__main__":
    # smoke test
    import sys
    ticker = sys.argv[1] if len(sys.argv) > 1 else "7203"
    mic = sys.argv[2] if len(sys.argv) > 2 else "XTKS"
    res = resolve_ticker_mic(ticker, mic)
    print(json.dumps(res.to_dict(), indent=2))

# --- END OF FILE ---
