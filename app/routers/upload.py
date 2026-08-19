"""POST /upload — ingest Sender/Receiver context PDFs.

Thin by design: this module orchestrates parsing.py + storage.py and shapes
the response. It doesn't know how PDFs get parsed or how Azure Storage works
— that separation is what keeps this file short and easy to walk through live.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, File, Form, UploadFile

from app.models import DocumentIngestResult, Role, UploadResponse
from app.parsing import PdfParseError, parse_pdf
from app.storage import save_document_metadata, upload_extracted_images, upload_raw_pdf

router = APIRouter()


@router.post("/upload", response_model=UploadResponse)
async def upload_context_pdfs(
    sender_id: str = Form(...),
    receiver_id: str = Form(...),
    role: Role = Form(...),  # which SIDE of the pair this batch of files describes — see docstring
    files: list[UploadFile] = File(...),
) -> UploadResponse:
    """Uploads one or more PDFs as context for a sender/receiver pair.

    All four fields are supplied by the caller on every request — none of
    them are inferred from the file itself. A real frontend (or Postman/
    Swagger, standing in for one here) is what decides these values; the
    code never guesses "this looks like a sender document."

    Why `role` exists separately from sender_id/receiver_id: those two just
    say *which campaign* this upload belongs to (e.g. the Northbridge <->
    Ferrow pairing) — they don't say which side of it this particular file
    represents. One call might upload Northbridge's own offering PDF
    (role="sender"); a separate call, same sender_id/receiver_id, uploads
    Ferrow's profile PDF (role="receiver"). /generate later needs to tell
    those apart — "what to sell" vs. "who to sell it to" are different
    prompts built from different content.

    sender_id / receiver_id are plain caller-chosen strings (e.g.
    "northbridge-analytics") — there is NO separate "register a company"
    step or a lookup table validating they're "real". Whatever string you
    pass becomes the pairing key: two upload calls using the same
    sender_id + receiver_id are what makes those documents belong to the
    same pair later, when /generate looks them up. A typo just silently
    creates a new, disconnected pair — acceptable for this prototype's
    single-tenant scope, but worth naming as a known gap if asked.

    Each file is ingested independently — one file failing to parse doesn't
    fail the whole batch, per the "fail loud, not silently" policy
    (decision-log.md §6): a failed file just gets status="failed" with an
    error message in its own result.
    """
    results = [await _ingest_one(sender_id, receiver_id, role, file) for file in files]
    return UploadResponse(results=results)


async def _ingest_one(sender_id: str, receiver_id: str, role: Role, file: UploadFile) -> DocumentIngestResult:
    document_id = uuid4()  # a fresh random UUID — just a unique ID, not derived from the file's content
    uploaded_at = datetime.now(timezone.utc)
    content = await file.read()  # raw PDF bytes straight from the uploaded file, nothing decoded yet

    # Store the raw PDF regardless of whether parsing succeeds, so a failed
    # upload can be inspected or retried without asking the user to re-upload.
    raw_pdf_blob_path = upload_raw_pdf(document_id, sender_id, receiver_id, role, file.filename, content)

    try:
        parsed = parse_pdf(content, file.filename)
    except PdfParseError as exc:
        return DocumentIngestResult(
            document_id=document_id,
            sender_id=sender_id,
            receiver_id=receiver_id,
            role=role,
            filename=file.filename,
            status="failed",
            error=str(exc),
            uploaded_at=uploaded_at,
        )

    image_blob_paths = upload_extracted_images(document_id, sender_id, receiver_id, role, parsed.images)

    result = DocumentIngestResult(
        document_id=document_id,
        sender_id=sender_id,
        receiver_id=receiver_id,
        role=role,
        filename=file.filename,
        status="parsed",
        pages_extracted=parsed.page_count,
        tables_extracted=len(parsed.tables),
        images_extracted=len(parsed.images),
        uploaded_at=uploaded_at,
    )
    save_document_metadata(result, parsed, raw_pdf_blob_path, image_blob_paths)
    return result
