from __future__ import annotations

from datetime import datetime, timezone

import requests

from modal_workers.shared.scanner_base import Signal, ScannerResult, run_scanner
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
