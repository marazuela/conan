from __future__ import annotations

from datetime import datetime, timezone

import requests

from modal_workers.shared.scanner_base import Signal, ScannerResult, _signal_to_row, run_scanner
from modal_workers.shared.supabase_client import ScannerConfig


class FakeClient:
    def __init__(self) -> None:
        self.closed = []
        self.updated = []
        self.close_exc: Exception | None = None
        self.update_exc: Exception | None = None
        self.raise_prefetch = False

    def load_scanner_config(self, scanner_name: str) -> ScannerConfig:
        return ScannerConfig(
            scanner_id="sc-1",
            name=scanner_name,
            status="operational",
            geography="US",
            cadence="daily",
            default_scoring_profile="activist_governance",
            signal_type_profile_map={},
            endpoints={},
            timeout_soft_s=60,
            timeout_hard_s=120,
            config={},
        )

    def open_scanner_run(self, scanner_id: str, modal_invocation_id: str | None = None) -> str:
        return "run-1"

    def prefetch_entities_by_figi(self, figis):
        if self.raise_prefetch:
            raise RuntimeError("prefetch blew up")
        return {}

    def resolve_or_create_entity(self, hints, prefetched=None) -> str:
        return "entity-1"

    def load_rubric_version_id(self, profile: str) -> str:
        return "rubric-1"

    def insert_signals(self, signals):
        return [signal["signal_id"] for signal in signals]

    def close_scanner_run(self, run_id: str, **kwargs) -> None:
        if self.close_exc is not None:
            raise self.close_exc
        self.closed.append({"run_id": run_id, **kwargs})

    def update_scanner_last_run(self, scanner_id: str, **kwargs) -> None:
        if self.update_exc is not None:
            raise self.update_exc
        self.updated.append({"scanner_id": scanner_id, **kwargs})


def _signal(**overrides) -> Signal:
    now = datetime.now(timezone.utc)
    base = {
        "signal_id": "sig-1",
        "source_content_hash": "sha256:test",
        "source_date": now,
        "scan_date": now,
        "signal_type": "test_signal",
        "raw_payload": {},
        "issuer_figi": "BBG000000001",
    }
    base.update(overrides)
    return Signal(**base)


def test_run_scanner_marks_post_scan_pipeline_failures_as_error():
    client = FakeClient()
    client.raise_prefetch = True

    def scan_fn(cfg: ScannerConfig) -> ScannerResult:
        return ScannerResult(
            scanner=cfg.name,
            status="ok",
            signals=[_signal()],
            fetched_records=1,
        )

    result = run_scanner("test_scanner", scan_fn, client=client)

    assert result.status == "error"
    assert result.error == "prefetch blew up"
    assert client.closed[0]["status"] == "error"
    assert client.updated[0]["last_run_status"] == "error"
    assert client.updated[0]["last_run_signals"] == 0


def test_run_scanner_warns_when_finalization_still_fails_after_retries():
    client = FakeClient()
    client.close_exc = requests.exceptions.ReadTimeout("close timeout")
    client.update_exc = requests.exceptions.ReadTimeout("update timeout")

    def scan_fn(cfg: ScannerConfig) -> ScannerResult:
        return ScannerResult(scanner=cfg.name, status="ok", signals=[], fetched_records=0)

    result = run_scanner("test_scanner", scan_fn, client=client)

    assert result.status == "partial"
    assert any("close_scanner_run failed after retries" in warning for warning in result.warnings)
    assert any("update_scanner_last_run failed after retries" in warning for warning in result.warnings)


# ----------------------------------------------------------------------
# scoring_meta — data_freshness stamping from market_snapshot liveness
# ----------------------------------------------------------------------

def _takeover_signal(**extra_payload) -> Signal:
    now = datetime.now(timezone.utc)
    payload = {
        "patterns_hit": 4,
        "pattern_names": ["strategic_review", "pe_take_private"],
        "primary_filing": {"file_date": now.strftime("%Y-%m-%d")},
        "pe_filer_type": "strategic",
        "pe_filer_name": "BigCorp Industries",
    }
    payload.update(extra_payload)
    return Signal(
        signal_id="sig-takeover",
        source_content_hash="sha256:takeover",
        source_date=now,
        scan_date=now,
        signal_type="takeover_candidate",
        scoring_profile="takeover_candidate",
        raw_payload=payload,
        issuer_figi="BBG000000009",
    )


def _cfg(profile: str = "takeover_candidate") -> ScannerConfig:
    return ScannerConfig(
        scanner_id="sc-2",
        name="test-scanner",
        status="operational",
        geography="US",
        cadence="daily",
        default_scoring_profile=profile,
        signal_type_profile_map={},
        endpoints={},
        timeout_soft_s=60,
        timeout_hard_s=120,
        config={},
    )


def test_signal_to_row_stamps_live_data_freshness_when_snapshot_is_fresh():
    client = FakeClient()
    sig = _takeover_signal(
        source_liveness="live",
        age_seconds=120,
        market_snapshot_source="yfinance",
    )
    row = _signal_to_row(sig, _cfg(), entity_id="ent-1", scanner_run_id="run-1", client=client)

    meta = row["extensions"]["scoring_meta"]
    assert meta["data_freshness"]["market_snapshot"]["status"] == "live"
    assert meta["data_freshness"]["market_snapshot"]["age_seconds"] == 120
    assert meta["data_freshness"]["market_snapshot"]["source"] == "yfinance"


def test_signal_to_row_stamps_stale_data_freshness_when_snapshot_is_stale_served():
    client = FakeClient()
    sig = _takeover_signal(
        source_liveness="stale_served",
        age_seconds=7200,
        market_snapshot_source="yfinance",
    )
    row = _signal_to_row(sig, _cfg(), entity_id="ent-1", scanner_run_id="run-1", client=client)

    meta = row["extensions"]["scoring_meta"]
    assert meta["data_freshness"]["market_snapshot"]["status"] == "stale_served"


def test_signal_to_row_stamps_missing_when_snapshot_unavailable():
    client = FakeClient()
    sig = _takeover_signal(
        source_liveness="unavailable",
        age_seconds=0,
        market_snapshot_source="yfinance",
    )
    row = _signal_to_row(sig, _cfg(), entity_id="ent-1", scanner_run_id="run-1", client=client)

    meta = row["extensions"]["scoring_meta"]
    assert meta["data_freshness"]["market_snapshot"]["status"] == "missing"


def test_signal_to_row_omits_data_freshness_when_no_snapshot_attempted():
    client = FakeClient()
    sig = _takeover_signal()  # no source_liveness key — scanner never called load_market_snapshot
    row = _signal_to_row(sig, _cfg(), entity_id="ent-1", scanner_run_id="run-1", client=client)

    meta = row["extensions"]["scoring_meta"]
    assert "data_freshness" not in meta
