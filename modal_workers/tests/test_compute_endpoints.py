"""
Tests for the compute-endpoint auth + storage upload guards in modal_workers.app.

These exercise the pure helpers (`_verify_compute_secret`,
`_validate_storage_upload`) that every compute endpoint calls before doing
work. The endpoint functions themselves are thin wrappers around these plus
a shared-module call, so the helpers carry all the guard logic.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from modal_workers.app import (
    ALLOWED_STORAGE_BUCKETS,
    MAX_STORAGE_CONTENT_BYTES,
    _validate_storage_upload,
    _verify_compute_secret,
)


# ---- _verify_compute_secret -----------------------------------------------


def test_verify_accepts_matching_secret(monkeypatch):
    monkeypatch.setenv("CONAN_COMPUTE_SECRET", "s3cret-abc")
    # Must not raise.
    _verify_compute_secret("s3cret-abc")


def test_verify_rejects_wrong_secret(monkeypatch):
    monkeypatch.setenv("CONAN_COMPUTE_SECRET", "s3cret-abc")
    with pytest.raises(HTTPException) as exc:
        _verify_compute_secret("wrong-value")
    assert exc.value.status_code == 401


def test_verify_rejects_missing_header(monkeypatch):
    monkeypatch.setenv("CONAN_COMPUTE_SECRET", "s3cret-abc")
    with pytest.raises(HTTPException) as exc:
        _verify_compute_secret(None)
    assert exc.value.status_code == 401


def test_verify_rejects_empty_header(monkeypatch):
    monkeypatch.setenv("CONAN_COMPUTE_SECRET", "s3cret-abc")
    with pytest.raises(HTTPException) as exc:
        _verify_compute_secret("")
    assert exc.value.status_code == 401


def test_verify_raises_500_when_server_secret_missing(monkeypatch):
    monkeypatch.delenv("CONAN_COMPUTE_SECRET", raising=False)
    with pytest.raises(HTTPException) as exc:
        _verify_compute_secret("anything")
    assert exc.value.status_code == 500


def test_verify_raises_500_when_server_secret_empty(monkeypatch):
    monkeypatch.setenv("CONAN_COMPUTE_SECRET", "")
    with pytest.raises(HTTPException) as exc:
        _verify_compute_secret("anything")
    assert exc.value.status_code == 500


# ---- _validate_storage_upload ---------------------------------------------


def _good_payload(**overrides):
    base = {
        "bucket": "reports",
        "path": "coverage/2026-W17.md",
        "content": "# coverage report\n",
    }
    base.update(overrides)
    return base


def test_validate_accepts_reports_bucket():
    _validate_storage_upload(_good_payload(bucket="reports"))


def test_validate_accepts_candidates_bucket():
    _validate_storage_upload(_good_payload(bucket="candidates", path="2026/04/ACME_signal-123.md"))


def test_validate_rejects_unknown_bucket():
    with pytest.raises(HTTPException) as exc:
        _validate_storage_upload(_good_payload(bucket="market-snapshots"))
    assert exc.value.status_code == 400
    assert exc.value.detail["error"] == "bucket not allowed"


def test_validate_rejects_missing_bucket():
    payload = _good_payload()
    del payload["bucket"]
    with pytest.raises(HTTPException) as exc:
        _validate_storage_upload(payload)
    assert exc.value.status_code == 400


def test_validate_rejects_empty_path():
    with pytest.raises(HTTPException) as exc:
        _validate_storage_upload(_good_payload(path=""))
    assert exc.value.status_code == 400


def test_validate_rejects_non_string_path():
    with pytest.raises(HTTPException) as exc:
        _validate_storage_upload(_good_payload(path=123))
    assert exc.value.status_code == 400


def test_validate_rejects_leading_slash_path():
    with pytest.raises(HTTPException) as exc:
        _validate_storage_upload(_good_payload(path="/coverage/oops.md"))
    assert exc.value.status_code == 400


def test_validate_rejects_path_traversal():
    with pytest.raises(HTTPException) as exc:
        _validate_storage_upload(_good_payload(path="coverage/../../etc/passwd"))
    assert exc.value.status_code == 400


def test_validate_rejects_non_string_content():
    with pytest.raises(HTTPException) as exc:
        _validate_storage_upload(_good_payload(content=b"bytes-not-str"))
    assert exc.value.status_code == 400


def test_validate_rejects_oversize_content():
    too_big = "x" * (MAX_STORAGE_CONTENT_BYTES + 1)
    with pytest.raises(HTTPException) as exc:
        _validate_storage_upload(_good_payload(content=too_big))
    assert exc.value.status_code == 413


def test_allowed_buckets_exactly_reports_and_candidates():
    # Guard against accidental scope creep — adding a bucket should be a
    # conscious change with its own test.
    assert ALLOWED_STORAGE_BUCKETS == frozenset({"reports", "candidates"})
