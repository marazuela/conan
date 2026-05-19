from datetime import datetime, timezone

from modal_workers.extractor.asset_linker import (
    MODEL as LINKER_MODEL,
    LinkerStats,
    record_linker_run_summary,
)
from modal_workers.extractor.sonnet_fact_extractor import (
    MODEL as FACT_MODEL,
    ExtractStats,
    record_fact_extractor_run_summary,
)


class FakeClient:
    def __init__(self):
        self.calls = []

    def _rest_with_retry(self, method, path, *, json_body=None, prefer=None, **kwargs):
        self.calls.append({
            "method": method,
            "path": path,
            "json_body": json_body,
            "prefer": prefer,
            "kwargs": kwargs,
        })
        return None


def test_record_linker_run_summary_payload():
    client = FakeClient()
    stats = LinkerStats(
        docs_seen=10,
        docs_prefilter_passed=7,
        docs_prefilter_skipped=3,
        docs_classified=7,
        links_inserted=2,
        links_dedup_skipped=1,
        api_calls=7,
        input_tokens=1234,
        output_tokens=56,
        cost_usd=0.123456,
        errors=1,
    )
    started_at = datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc)

    record_linker_run_summary(client, stats, started_at, status="completed")

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["method"] == "POST"
    assert call["path"] == "asset_linker_runs"
    assert call["prefer"] == "return=minimal"
    row = call["json_body"]
    assert row["pass"] == "pass1"
    assert row["model"] == LINKER_MODEL
    assert row["started_at"] == started_at.isoformat()
    assert row["status"] == "completed"
    assert row["docs_seen"] == 10
    assert row["prefilter_passed"] == 7
    assert row["prefilter_skipped"] == 3
    assert row["api_calls"] == 7
    assert row["errors"] == 1
    assert row["links_inserted"] == 2
    assert row["links_dedup_skipped"] == 1
    assert row["input_tokens"] == 1234
    assert row["output_tokens"] == 56
    assert row["cost_usd"] == 0.1235


def test_record_fact_extractor_run_summary_payload():
    client = FakeClient()
    stats = ExtractStats(
        docs_seen=5,
        docs_extracted=4,
        facts_inserted=12,
        api_calls=4,
        input_tokens=999,
        output_tokens=111,
        cost_usd=0.654321,
        errors=1,
    )
    started_at = datetime(2026, 5, 19, 12, 30, tzinfo=timezone.utc)

    record_fact_extractor_run_summary(
        client,
        stats,
        started_at,
        status="budget_exceeded",
        notes="dry_run=true",
    )

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["method"] == "POST"
    assert call["path"] == "fact_extractor_runs"
    assert call["prefer"] == "return=minimal"
    row = call["json_body"]
    assert row["model"] == FACT_MODEL
    assert row["started_at"] == started_at.isoformat()
    assert row["status"] == "budget_exceeded"
    assert row["docs_seen"] == 5
    assert row["docs_extracted"] == 4
    assert row["facts_inserted"] == 12
    assert row["api_calls"] == 4
    assert row["errors"] == 1
    assert row["input_tokens"] == 999
    assert row["output_tokens"] == 111
    assert row["cost_usd"] == 0.6543
    assert row["notes"] == "dry_run=true"
