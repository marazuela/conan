"""Unit tests for the reusable bc_pipeline_runs open/close helper (phase0 §5.1, phase1 §5).

The helper is the BC pipeline's single fail-loud liveness sink; Phase 0's enumerator is its
first consumer and Phase 1/2/3 import the same two functions. These tests pin the contract:

  - open_run POSTs status='running' + pipeline_name + snapshot_date, returns the row id,
    and asks for return=representation (so the id comes back).
  - close_run PATCHes the terminal status + finished_at + counts + cost_usd + log + reason
    to the right row, with return=minimal.
  - status domain is enforced IN PYTHON (CHECK-safety): a non-terminal close status raises
    ValueError before any DB call — the 'ok'/'error' tokens that 23514-failed earlier drafts
    can never reach the wire.
  - close_run is a no-op when run_id is falsy (so a crash before the row opened never raises
    a second, masking error inside the worker's finally).
  - the live CHECK domains (verified 2026-06-04) are mirrored as constants.
"""

from __future__ import annotations

import pytest

from modal_workers.shared.bc_pipeline_runs import (
    _ALL_STATUSES,
    _CLOSE_STATUSES,
    _OPEN_STATUS,
    close_run,
    open_run,
)

# Live CHECK domain mirror (bc_pipeline_runs_status_check, verified 2026-06-04).
_OK_STATUS = {"running", "succeeded", "failed", "partial"}

SNAP = "2026-06-04"


class FakeClient:
    """Records every _rest_with_retry call; emulates the INSERT ... RETURNING id."""

    def __init__(self, *, return_id: str = "run-xyz", insert_returns_empty: bool = False):
        self.calls = []
        self._return_id = return_id
        self._insert_returns_empty = insert_returns_empty

    def _rest_with_retry(self, method, path, *, json_body=None, prefer=None,
                         params=None, attempts=3, backoff_s=0.25):
        self.calls.append({"method": method, "path": path,
                           "json_body": json_body, "prefer": prefer})
        if method == "POST" and path == "bc_pipeline_runs":
            if self._insert_returns_empty:
                return []
            return [{"id": self._return_id}]
        return []


# ---------------------------------------------------------------------------
# Domain constants stay aligned with the live CHECK.
# ---------------------------------------------------------------------------

def test_status_domain_matches_live_check():
    assert _OPEN_STATUS == "running"
    assert _CLOSE_STATUSES == {"succeeded", "partial", "failed"}
    assert _ALL_STATUSES == _OK_STATUS


# ---------------------------------------------------------------------------
# open_run
# ---------------------------------------------------------------------------

def test_open_run_posts_running_and_returns_id():
    client = FakeClient(return_id="run-1")
    rid = open_run(client, pipeline_name="bc_universe_pdufa", snapshot_date=SNAP)
    assert rid == "run-1"
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["method"] == "POST"
    assert call["path"] == "bc_pipeline_runs"
    assert call["prefer"] == "return=representation"  # must read the generated id back
    body = call["json_body"][0]
    assert body["status"] == "running"
    assert body["status"] in _OK_STATUS
    assert body["pipeline_name"] == "bc_universe_pdufa"
    assert body["snapshot_date"] == SNAP
    assert body["started_at"]  # populated


def test_open_run_returns_none_when_no_representation():
    # If the insert returns no row (e.g. return=minimal misconfig), the id is None and the
    # caller treats it as "couldn't open" — close_run then no-ops.
    client = FakeClient(insert_returns_empty=True)
    rid = open_run(client, pipeline_name="bc_weekly_score", snapshot_date=SNAP)
    assert rid is None


def test_open_run_pipeline_name_is_caller_supplied():
    # The helper binds NO pipeline_name itself — Phase 1/2/3 pass their own.
    client = FakeClient()
    open_run(client, pipeline_name="bc_daily_monitor", snapshot_date=SNAP)
    assert client.calls[0]["json_body"][0]["pipeline_name"] == "bc_daily_monitor"


# ---------------------------------------------------------------------------
# close_run — happy paths
# ---------------------------------------------------------------------------

def test_close_run_succeeded_patches_terminal_row():
    client = FakeClient(return_id="run-7")
    rid = open_run(client, pipeline_name="bc_universe_pdufa", snapshot_date=SNAP)
    close_run(client, rid, status="succeeded", n_processed=18, n_failed=0,
              log={"N_in_window_pending_nda_bla": 18})
    patch = client.calls[-1]
    assert patch["method"] == "PATCH"
    assert patch["path"] == f"bc_pipeline_runs?id=eq.{rid}"
    assert patch["prefer"] == "return=minimal"
    pb = patch["json_body"]
    assert pb["status"] == "succeeded"
    assert pb["status"] in _OK_STATUS
    assert pb["n_processed"] == 18
    assert pb["n_failed"] == 0
    assert pb["cost_usd"] == 0          # default — no LLM on this path
    assert pb["finished_at"]
    assert pb["log"] == {"N_in_window_pending_nda_bla": 18}
    assert pb["reason"] is None


def test_close_run_failed_carries_reason():
    client = FakeClient(return_id="run-8")
    rid = open_run(client, pipeline_name="bc_universe_pdufa", snapshot_date=SNAP)
    close_run(client, rid, status="failed", n_processed=0, n_failed=0,
              log={"error": "boom"}, reason="RuntimeError: boom")
    pb = client.calls[-1]["json_body"]
    assert pb["status"] == "failed"
    assert pb["reason"].startswith("RuntimeError")


def test_close_run_partial_is_allowed():
    client = FakeClient(return_id="run-9")
    rid = open_run(client, pipeline_name="bc_universe_pdufa", snapshot_date=SNAP)
    close_run(client, rid, status="partial", n_processed=10, n_failed=3, log={})
    assert client.calls[-1]["json_body"]["status"] == "partial"


def test_close_run_cost_usd_passthrough():
    client = FakeClient(return_id="run-c")
    close_run(client, "run-c", status="succeeded", n_processed=1, n_failed=0,
              log={}, cost_usd=0.42)
    assert client.calls[-1]["json_body"]["cost_usd"] == 0.42


def test_close_run_defaults_log_to_empty_dict():
    client = FakeClient(return_id="run-d")
    close_run(client, "run-d", status="succeeded", n_processed=0, n_failed=0)
    assert client.calls[-1]["json_body"]["log"] == {}


# ---------------------------------------------------------------------------
# close_run — CHECK-safety (the constraint the earlier drafts violated)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["ok", "error", "running", "done", "succeed", "", "SUCCEEDED"])
def test_close_run_rejects_non_terminal_status_before_any_db_call(bad):
    client = FakeClient()
    with pytest.raises(ValueError):
        close_run(client, "run-1", status=bad, n_processed=0, n_failed=0, log={})
    # the guard fires BEFORE the PATCH — nothing hit the wire
    assert client.calls == []


def test_close_run_rejects_running_status_specifically():
    # 'running' is a valid OPEN status but NOT a valid CLOSE status — the helper must reject it.
    client = FakeClient()
    with pytest.raises(ValueError):
        close_run(client, "run-1", status="running", n_processed=0, n_failed=0, log={})


# ---------------------------------------------------------------------------
# close_run — no-op on falsy run_id (the finally-safety invariant)
# ---------------------------------------------------------------------------

def test_close_run_noop_when_run_id_none():
    client = FakeClient()
    close_run(client, None, status="succeeded", n_processed=0, n_failed=0, log={})
    assert client.calls == []  # nothing written — but no raise (safe inside finally)


def test_close_run_noop_when_run_id_empty_string():
    client = FakeClient()
    close_run(client, "", status="failed", n_processed=0, n_failed=0, log={},
              reason="opened-failed")
    assert client.calls == []


def test_close_run_status_validated_even_when_run_id_falsy():
    # status validity is checked FIRST, so a bad status still raises even with no run_id.
    client = FakeClient()
    with pytest.raises(ValueError):
        close_run(client, None, status="ok", n_processed=0, n_failed=0, log={})


# ---------------------------------------------------------------------------
# Full open→close round trip (the canonical fail-loud sequence).
# ---------------------------------------------------------------------------

def test_open_then_close_full_sequence_two_calls():
    client = FakeClient(return_id="run-seq")
    rid = open_run(client, pipeline_name="bc_universe_pdufa", snapshot_date=SNAP)
    close_run(client, rid, status="succeeded", n_processed=5, n_failed=0, log={"ok": True})
    assert len(client.calls) == 2
    assert client.calls[0]["method"] == "POST"
    assert client.calls[1]["method"] == "PATCH"
    assert f"id=eq.{rid}" in client.calls[1]["path"]
