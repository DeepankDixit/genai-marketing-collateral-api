"""POST /articles/{article_id}/revise — apply a customer's hand-edit to a previously generated
article, without trusting the client for anything beyond the fields it's actually allowed to
change.

Thin by design, same pattern as routers/generate.py: this module orchestrates storage.py (lookup +
persistence) and generation.py (revised-article assembly). It doesn't decide what's editable —
that's encoded in app.models.ArticleRevisionRequest.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from app.generation import assemble_revised_article
from app.models import ArticleListResponse, ArticleOutput, ArticleRevisionRequest
from app.pdf_export import build_pdf
from app.storage import get_article, list_articles, save_article

router = APIRouter()


@router.get("/articles", response_model=ArticleListResponse)
def get_articles(sender_id: str, receiver_id: str) -> ArticleListResponse:
    """Lists every version generated so far for one sender/receiver pair, newest first — both
    auto-generated (/generate) and hand-edited (/revise) rows, since both are stored the same
    way. Returns an empty list, not a 404, if nothing's been generated for this pair yet.
    """
    return ArticleListResponse(articles=list_articles(sender_id, receiver_id))


@router.get("/articles/{article_id}/pdf")
def export_article_pdf(article_id: UUID) -> Response:
    """Renders an already-generated article to a downloadable PDF. Read-only — never calls
    /generate, just looks up what's already stored (same get_article() lookup /revise uses) and
    hands it to pdf_export.build_pdf(). 404 if article_id doesn't match any stored article.
    """
    article = get_article(article_id)
    if article is None:
        raise HTTPException(status_code=404, detail=f"No article found with article_id={article_id}")

    pdf_bytes = build_pdf(article)
    filename = f"article-{article.article_id}-v{article.version}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/articles/{article_id}/revise", response_model=ArticleOutput)
def revise_article(article_id: UUID, revision: ArticleRevisionRequest) -> ArticleOutput:
    """Applies a hand-edit on top of an existing article and persists it as a new, separate
    version — same append-only pattern /generate already uses for regeneration (v1, v2, v3... all
    coexist). Raises 404 if article_id doesn't match any previously generated article.

    sender_id, receiver_id, theme, and every image slot's blob_path/image_url are always carried
    over from the original article, never taken from the request body — ArticleRevisionRequest
    doesn't define those fields, so there's nothing for the client to override even if it tried.
    """
    original = get_article(article_id)
    if original is None:
        raise HTTPException(status_code=404, detail=f"No article found with article_id={article_id}")

    revised = assemble_revised_article(original, revision)
    save_article(revised)
    return revised
