"""Stream 3.2 — Anthropic Files API upload test.

Run: python -m pytest modal_workers/tests/test_files_upload.py -v
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")
os.environ.setdefault("ANTHROPIC_ORCHESTRATOR_KEY", "test-key")

from modal_workers.shared.document_writer import DocumentWriter


def test_upload_returns_file_id_on_success():
    """Real path: anthropic SDK returns a file object with .id; we extract + return."""
    writer = DocumentWriter(client=MagicMock(), anthropic_api_key="test-key")
    fake_file = MagicMock()
    fake_file.id = "file_abc123"
    fake_files_api = MagicMock()
    fake_files_api.create.return_value = fake_file
    fake_anthropic_module = MagicMock()
    fake_anthropic_module.Anthropic.return_value.beta.files = fake_files_api

    with patch.dict("sys.modules", {"anthropic": fake_anthropic_module}):
        file_id = writer._upload_to_anthropic(b"%PDF-1.4 test", "test.pdf")

    assert file_id == "file_abc123"
    fake_files_api.create.assert_called_once()
    # Verify file tuple shape
    args = fake_files_api.create.call_args
    fname, body, mime = args.kwargs["file"]
    assert fname == "test.pdf"
    assert body == b"%PDF-1.4 test"
    assert mime == "application/pdf"


def test_upload_returns_none_on_anthropic_exception():
    """Failure path: SDK raises → return None, caller falls back to raw_text."""
    writer = DocumentWriter(client=MagicMock(), anthropic_api_key="test-key")
    fake_files_api = MagicMock()
    fake_files_api.create.side_effect = RuntimeError("rate-limited")
    fake_anthropic_module = MagicMock()
    fake_anthropic_module.Anthropic.return_value.beta.files = fake_files_api

    with patch.dict("sys.modules", {"anthropic": fake_anthropic_module}):
        file_id = writer._upload_to_anthropic(b"x", "test.pdf")

    assert file_id is None


def test_upload_returns_none_when_anthropic_module_missing():
    """anthropic SDK not installed → log and return None (don't crash)."""
    writer = DocumentWriter(client=MagicMock(), anthropic_api_key="test-key")
    # Force ImportError for the fresh import inside _upload_to_anthropic
    with patch.dict("sys.modules", {"anthropic": None}):
        file_id = writer._upload_to_anthropic(b"x", "t.pdf")
    assert file_id is None


def test_upload_falls_back_to_non_beta_files_api():
    """Some anthropic SDK versions expose files at .files (not .beta.files)."""
    writer = DocumentWriter(client=MagicMock(), anthropic_api_key="test-key")
    fake_file = MagicMock()
    fake_file.id = "file_xyz"
    fake_files_api = MagicMock()
    fake_files_api.create.return_value = fake_file
    fake_client = MagicMock(spec=["files"])  # no .beta attribute
    fake_client.files = fake_files_api
    fake_anthropic_module = MagicMock()
    fake_anthropic_module.Anthropic.return_value = fake_client
    # Strip .beta so the helper falls through to client.files
    del fake_client.beta

    with patch.dict("sys.modules", {"anthropic": fake_anthropic_module}):
        file_id = writer._upload_to_anthropic(b"x", "t.pdf")
    assert file_id == "file_xyz"
