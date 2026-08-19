"""PDF parsing: text, tables, and images out of raw PDF bytes.

Deliberately knows nothing about Azure or FastAPI — it takes bytes in and
returns a ParsedDocument. That keeps it trivially unit-testable and reusable
if the storage backend ever changes.

Prototype choice: pdfplumber (text + tables) + PyMuPDF/fitz (images), both
open-source and free to run. Azure Document Intelligence is the natural
swap-in for production-grade, layout-aware extraction — not used here to
keep the weekend build's cost and moving parts down (decision-log.md §17).

Output format note: everything below comes back as plain Python values, NOT
markdown and NOT base64. `text` is a plain string, `tables` is a nested list
of cell strings, and image `content` is the raw, already-decoded bytes of the
image file itself (e.g. real PNG bytes — the same bytes as if you'd saved
that image on disk). Nothing here converts the page to markdown or
base64-encodes anything; that's a different kind of pipeline (some
vision-model/markdown-based PDF tools work that way, this one doesn't).
Base64 would only matter if we needed to embed binary data inside JSON/text —
we don't, because images go straight to Blob Storage as binary blobs.
"""
from __future__ import annotations

import io

import fitz  # PyMuPDF
import pdfplumber

from app.models import ExtractedImage, ParsedDocument


class PdfParseError(Exception):
    """Raised on any parse failure so the caller can fail loud, per decision-log.md §6."""


def parse_pdf(content: bytes, filename: str) -> ParsedDocument:
    """Extract text, tables, and images from PDF bytes.

    Raises PdfParseError for anything unparseable (corrupt, encrypted, not
    actually a PDF, etc.) rather than returning a partial/empty result.
    """
    try:
        text, tables, page_count = _extract_text_and_tables(content)
        images = _extract_images(content, filename)
    except Exception as exc:  # noqa: BLE001 — any failure here should fail loud, not degrade silently
        raise PdfParseError(f"Could not parse '{filename}': {exc}") from exc

    return ParsedDocument(text=text, tables=tables, page_count=page_count, images=images)


def _extract_text_and_tables(content: bytes) -> tuple[str, list[list[list[str]]], int]:
    """Returns (full_text, tables, page_count). Internal helper — not used outside this file.

    pdfplumber.open() wants a file path or a file-like object, not raw bytes
    directly — io.BytesIO(content) just wraps our in-memory bytes so it looks
    like an open file to pdfplumber, with nothing written to disk.

    page.extract_text() reads every character's position on the page and
    stitches them back into reading-order text.

    page.extract_tables() is a layout heuristic, not OCR and not an LLM: it
    looks at the page's ruled lines (or, if there are none, at consistent
    gaps/alignment between text tokens) to infer a row/column grid, then
    assigns each text token to a cell based on where it sits in that grid.
    This works reliably here because our mock PDFs are born-digital with
    real vector-drawn table borders (built with reportlab) — a scanned PDF
    would need OCR first, which this function does not do.
    Returns one entry per table found on the page; each table is itself a
    list of rows, each row a list of cell strings — e.g.
    tables[0] == [["Capability", "Northbridge...", "Typical..."], [...], ...].
    """
    text_parts: list[str] = []
    tables: list[list[list[str]]] = []

    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            text_parts.append(page.extract_text() or "")
            tables.extend(page.extract_tables())
        page_count = len(pdf.pages)

    return "\n".join(text_parts), tables, page_count


def _extract_images(content: bytes, filename: str) -> list[ExtractedImage]:
    """Returns one ExtractedImage per raster image embedded in the PDF.

    fitz.open(stream=..., filetype="pdf") is PyMuPDF's entry point for
    opening a PDF from in-memory bytes (filetype="pdf" is needed because we
    have no filename/extension to infer it from) — returns a Document.

    Iterating a Document yields one Page per page, in page order;
    enumerate(doc, start=1) just numbers them 1, 2, 3... instead of 0, 1, 2
    for human-readable output.

    page.get_images(full=True) scans that page's content stream and returns
    one tuple per embedded raster image it references (full=True also
    catches images nested inside form XObjects, not just directly-placed
    ones). Each tuple's first element, img[0], is the image's "xref" — an
    integer that points to that image's object inside the PDF file's
    internal object table. It is NOT the image data itself, just a pointer
    to it.

    doc.extract_image(xref) follows that pointer and returns a dict with:
      - "image": the actual image file bytes, already decoded to a normal
        format (e.g. real PNG/JPEG bytes — the same bytes you'd get saving
        that picture as its own file). Not base64, not markdown.
      - "ext": the matching file extension string (e.g. "png").
    """
    images: list[ExtractedImage] = []

    with fitz.open(stream=content, filetype="pdf") as doc:
        for page_number, page in enumerate(doc, start=1):
            for image_index, img in enumerate(page.get_images(full=True), start=1):
                xref = img[0]
                base_image = doc.extract_image(xref)
                # Filename embeds source file + page + index so multiple
                # images across multiple files/pages never collide once
                # they're all uploaded into the same Blob container.
                image_filename = f"{filename}_p{page_number}_{image_index}.{base_image['ext']}"
                images.append(
                    ExtractedImage(
                        filename=image_filename,
                        content=base_image["image"],
                        page_number=page_number,
                    )
                )

    return images
