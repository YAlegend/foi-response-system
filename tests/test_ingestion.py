"""Unit tests for ingestion helpers that don't need the network."""
from __future__ import annotations

from app.ingestion.website_crawler import (_extract_pdf_text, _is_pdf_response,
                                           _pdf_title)


def test_is_pdf_response_by_content_type_and_magic_bytes():
    assert _is_pdf_response("application/pdf; charset=binary", b"anything")
    assert _is_pdf_response("application/octet-stream", b"%PDF-1.7\n...")
    # A .pdf URL that actually serves HTML must NOT be treated as a PDF, else the
    # page would be dropped instead of ingested as HTML (real council behaviour).
    assert not _is_pdf_response("text/html; charset=utf-8", b"<!DOCTYPE html>")
    assert not _is_pdf_response("", b"")


def test_extract_pdf_text_is_graceful_on_non_pdf():
    # Garbage bytes must never raise — they just yield no text, so the doc falls
    # below the content floor and is skipped.
    assert _extract_pdf_text(b"this is not a pdf") == ""
    assert _extract_pdf_text(b"") == ""


def test_pdf_title_prefers_first_substantial_line():
    raw = "  \n2024/25 Annual Budget Report\nHertfordshire County Council\n..."
    assert _pdf_title(raw, "https://x/doc.pdf") == "2024/25 Annual Budget Report"


def test_pdf_title_falls_back_to_tidied_filename():
    # No usable text (e.g. scanned PDF) -> derive a title from the URL filename.
    assert _pdf_title("", "https://x.gov.uk/docs/Council-Budget_2025-26.pdf") == \
        "Council Budget 2025 26"


def test_extract_text_from_minimal_docx():
    import io
    import zipfile

    from app.ingestion.documents import extract_text
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml",
                   "<w:document><w:body><w:p><w:r><w:t>Budget is 1.3 million "
                   "pounds.</w:t></w:r></w:p></w:body></w:document>")
    assert "1.3 million pounds" in extract_text("budget.docx", buf.getvalue())


def test_extract_text_plain_and_unsupported():
    from app.ingestion.documents import extract_text
    assert extract_text("note.txt", b"  hello   world  ") == "hello world"
    assert extract_text("x.exe", b"MZ\x00\x00") == ""     # unsupported -> empty
    assert extract_text("photo.png", b"\x89PNG\r\n") == ""
