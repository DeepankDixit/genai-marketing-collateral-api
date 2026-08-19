"""POST /generate — produce a factually-grounded article for a sender/receiver pair.

Thin by design, same pattern as routers/upload.py: this module orchestrates storage.py (context
lookup + persistence) and generation.py (prompting, LLM call, retry, assembly). It doesn't know how
prompts are built or how Azure OpenAI is called — that separation is what keeps this file short.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.generation import assemble_article, generate_draft_with_retry, resolve_image_slots
from app.models import ArticleOutput, GenerateRequest
from app.storage import fetch_context, save_article

router = APIRouter()


@router.post("/generate", response_model=ArticleOutput)
def generate_article(request: GenerateRequest) -> ArticleOutput:
    """Generates one article for an already-uploaded sender/receiver pair.

    sender_id/receiver_id must match an earlier POST /upload for each role — this endpoint never
    ingests PDFs itself, it only reads what /upload already stored. Raises 404 if either side has
    no uploaded context yet, and 502 if the LLM never produced schema-valid output within
    generate_draft_with_retry's retry budget.

    request.feedback is an optional plain string, same shape as a future /evaluate response's
    feedback field (decision-log.md §9) — this endpoint never calls /evaluate itself, it just
    accepts feedback back in if the caller supplies it, for a regenerate-with-feedback round trip.
    """
    try:
        sender_ctx = fetch_context(request.sender_id, request.receiver_id, role="sender")
        receiver_ctx = fetch_context(request.sender_id, request.receiver_id, role="receiver")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    try:
        draft, _attempts = generate_draft_with_retry(sender_ctx, receiver_ctx, feedback=request.feedback)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    image_slots = resolve_image_slots(sender_ctx, receiver_ctx, draft)
    article = assemble_article(request, draft, image_slots)
    save_article(article)
    return article
