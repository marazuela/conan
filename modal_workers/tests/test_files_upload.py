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
    """Real path (modern SDK): anthropic SDK returns a file object with .id via
    files.upload(); we extract + return."""
    writer = DocumentWriter(client=MagicMock(), anthropic_api_key="test-key")
    fake_file = MagicMock()
    fake_file.id = "file_abc123"
    fake_files_api = MagicMock()
    fake_files_api.upload.return_value = fake_file
    fake_anthropic_module = MagicMock()
    fake_anthropic_module.Anthropic.return_value.beta.files = fake_files_api

    with patch.dict("sys.modules", {"anthropic": fake_anthropic_module}):
        file_id = writer._upload_to_anthropic(
            b"%PDF-1.4 test", "test.pdf", is_pdf=True,
        )

    assert file_id == "file_abc123"
    fake_files_api.upload.assert_called_once()
    # Verify file tuple shape
    args = fake_files_api.upload.call_args
    fname, body, mime = args.kwargs["file"]
    assert fname == "test.pdf"
    assert body == b"%PDF-1.4 test"
    assert mime == "application/pdf"


def test_upload_falls_back_to_create_when_upload_missing():
    """Legacy SDK path: older anthropic SDKs exposed files.create() instead of
    files.upload(). Our helper must transparently fall through."""
    writer = DocumentWriter(client=MagicMock(), anthropic_api_key="test-key")
    fake_file = MagicMock()
    fake_file.id = "file_legacy"
    # spec=['create'] makes .upload not exist on the mock, forcing fallback.
    fake_files_api = MagicMock(spec=["create"])
    fake_files_api.create.return_value = fake_file
    fake_anthropic_module = MagicMock()
    fake_anthropic_module.Anthropic.return_value.beta.files = fake_files_api

    with patch.dict("sys.modules", {"anthropic": fake_anthropic_module}):
        file_id = writer._upload_to_anthropic(
            b"%PDF-1.4 test", "test.pdf", is_pdf=True,
        )

    assert file_id == "file_legacy"
    fake_files_api.create.assert_called_once()


def test_upload_returns_none_on_anthropic_exception():
    """Failure path: SDK raises → return None, caller falls back to raw_text."""
    writer = DocumentWriter(client=MagicMock(), anthropic_api_key="test-key")
    fake_files_api = MagicMock()
    fake_files_api.upload.side_effect = RuntimeError("rate-limited")
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
    fake_files_api.upload.return_value = fake_file
    fake_client = MagicMock(spec=["files"])  # no .beta attribute
    fake_client.files = fake_files_api
    fake_anthropic_module = MagicMock()
    fake_anthropic_module.Anthropic.return_value = fake_client
    # Strip .beta so the helper falls through to client.files
    del fake_client.beta

    with patch.dict("sys.modules", {"anthropic": fake_anthropic_module}):
        file_id = writer._upload_to_anthropic(b"x", "t.pdf")
    assert file_id == "file_xyz"


def test_upload_uses_text_plain_mime_when_not_pdf():
    """Text payloads (is_pdf=False) must travel as text/plain so Anthropic's
    citation walker indexes them line-by-line rather than as a PDF blob."""
    writer = DocumentWriter(client=MagicMock(), anthropic_api_key="test-key")
    fake_file = MagicMock()
    fake_file.id = "file_text_1"
    fake_files_api = MagicMock()
    fake_files_api.upload.return_value = fake_file
    fake_anthropic_module = MagicMock()
    fake_anthropic_module.Anthropic.return_value.beta.files = fake_files_api

    with patch.dict("sys.modules", {"anthropic": fake_anthropic_module}):
        file_id = writer._upload_to_anthropic(
            b"large body of structured text " * 1000,
            "edgar_10K.txt",
            is_pdf=False,
        )

    assert file_id == "file_text_1"
    fname, _body, mime = fake_files_api.upload.call_args.kwargs["file"]
    assert fname == "edgar_10K.txt"
    assert mime == "text/plain"


def test_upload_uses_pdf_mime_when_is_pdf_true():
    """PDF payloads must travel as application/pdf for native document blocks."""
    writer = DocumentWriter(client=MagicMock(), anthropic_api_key="test-key")
    fake_file = MagicMock()
    fake_file.id = "file_pdf_1"
    fake_files_api = MagicMock()
    fake_files_api.upload.return_value = fake_file
    fake_anthropic_module = MagicMock()
    fake_anthropic_module.Anthropic.return_value.beta.files = fake_files_api

    with patch.dict("sys.modules", {"anthropic": fake_anthropic_module}):
        writer._upload_to_anthropic(b"%PDF-1.4 ...", "advisory.pdf", is_pdf=True)

    _fname, _body, mime = fake_files_api.upload.call_args.kwargs["file"]
    assert mime == "application/pdf"


def test_size_gate_skips_small_docs():
    """write_document must NOT call _upload_to_anthropic for docs below
    MIN_UPLOAD_BYTES, even when upload_to_anthropic=True. The text-fallback
    path in Stage-1 handles small docs without truncation, so an upload
    round-trip is pure overhead."""
    from datetime import datetime, timezone

    from modal_workers.shared.document_writer import MIN_UPLOAD_BYTES

    fake_supabase = MagicMock()
    fake_supabase._rest.return_value = [{
        "id": "doc-id-1",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "storage_path": None,
        "anthropic_file_id": None,
    }]
    writer = DocumentWriter(client=fake_supabase, anthropic_api_key="test-key")

    small_body = "x" * 1000  # well below MIN_UPLOAD_BYTES (=20000)
    assert len(small_body.encode("utf-8")) < MIN_UPLOAD_BYTES

    with patch.object(writer, "_upload_to_anthropic") as mocked_upload:
        result = writer.write_document(
            source="edgar",
            source_doc_id="0000000000-25-000001",
            doc_type="8-K",
            raw_text=small_body,
            published_at=datetime.now(timezone.utc),
            is_pdf=False,
            upload_to_anthropic=True,
        )

    mocked_upload.assert_not_called()
    assert result.anthropic_file_id is None


def test_size_gate_uploads_large_docs():
    """write_document must call _upload_to_anthropic when raw_text is at or
    above MIN_UPLOAD_BYTES and upload_to_anthropic=True."""
    from datetime import datetime, timezone

    from modal_workers.shared.document_writer import MIN_UPLOAD_BYTES

    fake_supabase = MagicMock()
    fake_supabase._rest.return_value = [{
        "id": "doc-id-2",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "storage_path": None,
        "anthropic_file_id": "file_returned",
    }]
    writer = DocumentWriter(client=fake_supabase, anthropic_api_key="test-key")

    big_body = "y" * (MIN_UPLOAD_BYTES + 100)

    with patch.object(
        writer, "_upload_to_anthropic", return_value="file_returned"
    ) as mocked_upload:
        writer.write_document(
            source="edgar",
            source_doc_id="0000000000-25-000002",
            doc_type="10-K",
            raw_text=big_body,
            published_at=datetime.now(timezone.utc),
            is_pdf=False,
            upload_to_anthropic=True,
        )

    mocked_upload.assert_called_once()
    # The text-doc path must request a .txt extension (not .pdf) so the
    # filename hint downstream is consistent with the MIME type.
    _body_arg, filename_arg = mocked_upload.call_args.args[:2]
    assert filename_arg.endswith(".txt")
    assert mocked_upload.call_args.kwargs.get("is_pdf") is False


def test_size_gate_pdf_path_keeps_pdf_extension():
    """When is_pdf=True and the body is large enough, the filename hint
    should still end .pdf (not .txt)."""
    from datetime import datetime, timezone

    from modal_workers.shared.document_writer import MIN_UPLOAD_BYTES

    fake_supabase = MagicMock()
    fake_supabase._rest.return_value = [{
        "id": "doc-id-3",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "storage_path": None,
        "anthropic_file_id": "file_pdf_returned",
    }]
    writer = DocumentWriter(client=fake_supabase, anthropic_api_key="test-key")

    big_body = "z" * (MIN_UPLOAD_BYTES + 50)

    with patch.object(
        writer, "_upload_to_anthropic", return_value="file_pdf_returned"
    ) as mocked_upload:
        writer.write_document(
            source="fda_advisory",
            source_doc_id="ADV-2026-0001",
            doc_type="advisory_letter",
            raw_text=big_body,
            published_at=datetime.now(timezone.utc),
            is_pdf=True,
            upload_to_anthropic=True,
        )

    _body_arg, filename_arg = mocked_upload.call_args.args[:2]
    assert filename_arg.endswith(".pdf")
    assert mocked_upload.call_args.kwargs.get("is_pdf") is True
