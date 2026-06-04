"""Extract searchable text from an uploaded document.

Used by the knowledge-base upload endpoint so a subject department can contribute
a report/letter/spreadsheet-export and have its text grounded on by the drafter.
Supported types: PDF, Word (.docx), plain text (.txt/.md) and HTML. Everything is
parsed with libraries already present (pypdf) or the standard library (.docx is a
zip of XML), so uploads need no extra runtime dependency.

Returns ('', ...) for unsupported or unreadable files; the caller rejects empties
so a scanned/image-only or corrupt file fails with a clear message rather than
storing an empty document.
"""
from __future__ import annotations

import io
import re
import zipfile

# Reuse the crawler's hardened PDF extractor (handles missing pypdf + bad bytes).
from .website_crawler import _extract_pdf_text

SUPPORTED = {".pdf", ".docx", ".txt", ".md", ".markdown", ".html", ".htm"}


def _suffix(filename: str) -> str:
    name = (filename or "").lower().strip()
    dot = name.rfind(".")
    return name[dot:] if dot != -1 else ""


def _extract_docx_text(data: bytes) -> str:
    """Pull text from a .docx (a zip of XML) using only the standard library.
    Joins the text of every <w:t> run with spaces; returns '' on any error."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("word/document.xml").decode("utf-8", errors="ignore")
    except Exception:
        return ""
    # Treat paragraph/break boundaries as spaces, then keep only run text <w:t>.
    xml = re.sub(r"</w:p>|<w:br/>|<w:tab/>", " ", xml)
    runs = re.findall(r"<w:t[^>]*>(.*?)</w:t>", xml, re.S)
    text = " ".join(runs)
    # Unescape the handful of XML entities Word emits.
    for ent, ch in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                    ("&quot;", '"'), ("&apos;", "'")):
        text = text.replace(ent, ch)
    return " ".join(text.split())


def _extract_html_text(data: bytes) -> str:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return data.decode("utf-8", errors="ignore")
    soup = BeautifulSoup(data.decode("utf-8", errors="ignore"), "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return " ".join(soup.get_text(" ").split())


def extract_text(filename: str, data: bytes) -> str:
    """Return the plain text of an uploaded file, or '' if unsupported/unreadable."""
    suffix = _suffix(filename)
    if suffix == ".pdf":
        return " ".join(_extract_pdf_text(data).split())
    if suffix == ".docx":
        return _extract_docx_text(data)
    if suffix in {".html", ".htm"}:
        return _extract_html_text(data)
    if suffix in {".txt", ".md", ".markdown"}:
        return " ".join(data.decode("utf-8", errors="ignore").split())
    return ""
